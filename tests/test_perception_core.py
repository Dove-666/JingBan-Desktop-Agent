"""Deterministic perception policy tests; no screen capture, network, or files."""

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import unittest

from PySide6.QtGui import QColor, QImage

from desktop_pet.perception.change_detector import ChangeDetector
from desktop_pet.perception.event_deduplicator import EventDeduplicator
from desktop_pet.perception.proactive_agent import Decision, ProactiveAgent
from desktop_pet.perception.settings import PerceptionSettings


def image(color=0, width=160, height=90):
    result = QImage(width, height, QImage.Format.Format_RGB32)
    result.fill(QColor(color, color, color) if isinstance(color, int) else color)
    return result


def event(**overrides):
    fields = dict(kind="activity", summary="用户完成了一项工作", reply="辛苦啦！",
                  emotion="happy", importance=0.8, confidence=0.9, event_key="task-complete")
    fields.update(overrides)
    return SimpleNamespace(**fields)


class PerceptionSettingsTests(unittest.TestCase):
    def test_requested_screen_defaults_retain_privacy_filters(self):
        settings = PerceptionSettings()
        settings.validate()
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.allow_remote)
        self.assertEqual((settings.observation_interval_seconds, settings.analysis_interval_seconds,
                          settings.change_threshold, settings.speech_cooldown_seconds,
                          settings.max_speeches_per_hour, settings.idle_pause_seconds),
                         (10, 30, .08, 30, 30, 585))
        self.assertIn("密码", settings.privacy_keywords)
        self.assertIn("incognito", settings.privacy_keywords)
        self.assertIn("bitwarden.exe", settings.excluded_apps)
        with self.assertRaises(FrozenInstanceError):
            settings.enabled = True

    def test_each_numeric_bound_is_enforced_including_nonfinite_values(self):
        cases = (
            ("observation_interval_seconds", (4, 31, True, 10.5)),
            ("analysis_interval_seconds", (29, 601, "60")),
            ("change_threshold", (0, 0.009, 1.1, float("nan"), float("inf"), True)),
            ("speech_cooldown_seconds", (29, 1801)),
            ("max_speeches_per_hour", (0, 31, False)),
            ("idle_pause_seconds", (59, 7201)),
        )
        for field, values in cases:
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(PerceptionSettings(), **{field: value}).validate()

    def test_numeric_endpoints_are_valid(self):
        PerceptionSettings(observation_interval_seconds=5, analysis_interval_seconds=30,
                           change_threshold=.01, speech_cooldown_seconds=30,
                           max_speeches_per_hour=1, idle_pause_seconds=60).validate()
        PerceptionSettings(observation_interval_seconds=30, analysis_interval_seconds=600,
                           change_threshold=1, speech_cooldown_seconds=1800,
                           max_speeches_per_hour=30, idle_pause_seconds=7200).validate()

    def test_commentary_mode_defaults_to_companion_and_rejects_unknown_modes(self):
        self.assertEqual(PerceptionSettings().commentary_mode, "companion")
        for mode in ("companion", "quiet"):
            replace(PerceptionSettings(), commentary_mode=mode).validate()
        for mode in ("always_speak", "", None, [], True):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                replace(PerceptionSettings(), commentary_mode=mode).validate()

    def test_keywords_and_apps_accept_tuples_and_reject_ambiguous_values(self):
        replace(PerceptionSettings(), privacy_keywords=(), excluded_apps=("custom.exe",)).validate()
        for field in ("privacy_keywords", "excluded_apps"):
            for value in (["password"], "password", ("",), ("bad\nword",), (42,)):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(PerceptionSettings(), **{field: value}).validate()
        with self.assertRaises(ValueError):
            PerceptionSettings(enabled=1).validate()


class ChangeDetectorTests(unittest.TestCase):
    def test_first_image_is_new_until_explicitly_accepted(self):
        detector = ChangeDetector()
        source = image()
        first = detector.evaluate(source, "editor.exe")
        self.assertTrue(first.changed)
        self.assertEqual(first.reason, "initial")
        self.assertEqual(len(first.fingerprint), 64)
        self.assertEqual(detector.evaluate(source, "editor.exe"), first)
        detector.accept(source, "editor.exe")
        result = detector.evaluate(source, "editor.exe")
        self.assertFalse(result.changed)
        self.assertEqual(result.score, 0)
        self.assertEqual(result.fingerprint, first.fingerprint)

    def test_small_noise_is_ignored_and_gradual_changes_accumulate(self):
        detector = ChangeDetector(threshold=.08)
        detector.accept(image(0), "editor.exe")
        self.assertFalse(detector.evaluate(image(10), "editor.exe").changed)
        self.assertFalse(detector.evaluate(image(20), "editor.exe").changed)
        result = detector.evaluate(image(30), "editor.exe")
        self.assertTrue(result.changed)
        self.assertEqual(result.reason, "scene_changed")
        self.assertAlmostEqual(result.score, 30 / 255)

    def test_full_change_is_normalized_to_one(self):
        detector = ChangeDetector()
        detector.accept(image(0), "editor.exe")
        self.assertEqual(detector.evaluate(image(255), "editor.exe").score, 1)

    def test_color_changes_are_visible_even_with_similar_luminance(self):
        detector = ChangeDetector()
        detector.accept(image(QColor(255, 0, 0)), "editor.exe")
        self.assertTrue(detector.evaluate(image(QColor(0, 130, 0)), "editor.exe").changed)

    def test_application_switch_and_geometry_change_are_independent_signals(self):
        detector = ChangeDetector()
        detector.accept(image(), "Editor.EXE")
        self.assertFalse(detector.evaluate(image(), "editor.exe").changed)
        changed = detector.evaluate(image(), "browser.exe")
        self.assertTrue(changed.changed)
        self.assertEqual(changed.reason, "application_changed")
        resized = detector.evaluate(image(width=100), "editor.exe")
        self.assertTrue(resized.changed)
        self.assertEqual(resized.reason, "geometry_changed")

    def test_null_image_neither_triggers_nor_overwrites_baseline(self):
        detector = ChangeDetector()
        detector.accept(image(), "editor.exe")
        for invalid in (QImage(), None):
            self.assertFalse(detector.evaluate(invalid, "editor.exe").changed)
            self.assertEqual(detector.evaluate(invalid, "editor.exe").reason, "empty_image")
            detector.accept(invalid, "editor.exe")
        self.assertFalse(detector.evaluate(image(), "editor.exe").changed)

    def test_odd_scanline_sizes_have_stable_fingerprints(self):
        detector = ChangeDetector(sample_size=(5, 3))
        detector.accept(image(42), "editor.exe")
        results = [detector.evaluate(image(42), "editor.exe") for _ in range(10)]
        self.assertTrue(all(result.score == 0 for result in results))
        self.assertEqual(len({result.fingerprint for result in results}), 1)

    def test_accept_and_reset_update_only_baseline(self):
        detector = ChangeDetector()
        detector.accept(image(0), "editor.exe")
        detector.accept(image(255), "editor.exe")
        self.assertFalse(detector.evaluate(image(255), "editor.exe").changed)
        detector.reset()
        self.assertEqual(detector.evaluate(image(255), "editor.exe").reason, "initial")

    def test_configuration_rejects_invalid_threshold_or_sample_dimensions(self):
        for value in (0, float("nan"), 1.01, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ChangeDetector(value)
        for value in ((0, 3), (257, 3), (3.5, 3), (True, 3), [3, 3]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ChangeDetector(sample_size=value)
        detector = ChangeDetector()
        detector.threshold = 1
        detector.accept(image(0), "app")
        self.assertFalse(detector.evaluate(image(254), "app").changed)
        self.assertTrue(detector.evaluate(image(255), "app").changed)


class EventDeduplicatorTests(unittest.TestCase):
    def test_only_remembered_events_are_duplicates(self):
        dedup = EventDeduplicator()
        self.assertFalse(dedup.is_duplicate("task", 0))
        self.assertFalse(dedup.is_duplicate("task", 1))
        dedup.remember("task", 2)
        self.assertTrue(dedup.is_duplicate("task", 3))
        self.assertFalse(dedup.is_duplicate("different-task", 3))

    def test_ttl_does_not_extend_on_lookup(self):
        dedup = EventDeduplicator()
        dedup.remember("task", 0)
        self.assertTrue(dedup.is_duplicate("task", 599))
        self.assertFalse(dedup.is_duplicate("task", 600))

    def test_lru_is_bounded_and_lookup_refreshes_eviction_order(self):
        dedup = EventDeduplicator(max_entries=2)
        dedup.remember("a", 0)
        dedup.remember("b", 1)
        self.assertTrue(dedup.is_duplicate("a", 2))
        dedup.remember("c", 3)
        self.assertFalse(dedup.is_duplicate("b", 4))
        self.assertTrue(dedup.is_duplicate("a", 4))
        self.assertTrue(dedup.is_duplicate("c", 4))

    def test_remember_refreshes_ttl_and_custom_ttl_is_supported(self):
        dedup = EventDeduplicator(ttl_seconds=10)
        dedup.remember("a", 0)
        dedup.remember("a", 9)
        self.assertTrue(dedup.is_duplicate("a", 10))
        self.assertFalse(dedup.is_duplicate("a", 19))
        dedup.remember("b", 20)
        self.assertFalse(dedup.is_duplicate("b", 22, ttl_seconds=2))

    def test_empty_keys_are_not_all_collapsed_into_one_event(self):
        dedup = EventDeduplicator()
        for key in ("", " ", None):
            dedup.remember(key, 0)
            self.assertFalse(dedup.is_duplicate(key, 0))

    def test_clock_rollback_and_explicit_reset_release_stale_events(self):
        dedup = EventDeduplicator()
        dedup.remember("a", 100)
        self.assertFalse(dedup.is_duplicate("a", 0))
        dedup.remember("b", 1)
        dedup.reset()
        self.assertFalse(dedup.is_duplicate("b", 2))

    def test_invalid_limits_and_nonfinite_clock_are_rejected(self):
        for capacity in (0, True, 1.5):
            with self.assertRaises(ValueError):
                EventDeduplicator(max_entries=capacity)
        for duration in (0, -1, float("nan"), True):
            with self.assertRaises(ValueError):
                EventDeduplicator(ttl_seconds=duration)
        with self.assertRaises(ValueError):
            EventDeduplicator().remember("event", float("inf"))


class ProactiveAgentTests(unittest.TestCase):
    def setUp(self):
        self.settings = PerceptionSettings(enabled=True, speech_cooldown_seconds=180,
                                           max_speeches_per_hour=6, idle_pause_seconds=600)
        self.agent = ProactiveAgent()

    def test_disabled_busy_and_long_idle_are_quiet(self):
        self.assertEqual(self.agent.decide(event(), 0, PerceptionSettings(enabled=False)).reason, "disabled")
        self.assertEqual(self.agent.decide(event(), 0, self.settings, busy=True).reason, "user_busy")
        self.assertEqual(self.agent.decide(event(), 0, self.settings, idle_seconds=600).reason, "user_idle")
        self.assertEqual(self.agent.decide(event(), 0, self.settings, idle_seconds=599).action, "speak")

    def test_low_confidence_is_quiet_and_low_importance_only_moves(self):
        self.assertEqual(self.agent.decide(event(confidence=.59), 0, self.settings).action, "quiet")
        self.assertEqual(self.agent.decide(event(confidence=.6, importance=.2), 0, self.settings).action, "motion")
        self.assertEqual(self.agent.decide(event(importance=.65), 0, self.settings).action, "speak")

    def test_companion_comments_on_clear_media_without_faking_high_importance(self):
        for kind in ("media", "interesting"):
            with self.subTest(kind=kind):
                source = event(kind=kind, importance=.1, confidence=.8,
                               reply="这个小零食的包装看起来很有趣呀。")
                self.assertEqual(self.agent.decide(source, 0, self.settings),
                                 Decision("speak", "companion_event"))
                self.assertEqual(source.importance, .1)

    def test_quiet_mode_preserves_important_event_threshold(self):
        settings = replace(self.settings, commentary_mode="quiet")
        for kind in ("media", "interesting"):
            with self.subTest(kind=kind):
                self.assertEqual(self.agent.decide(event(kind=kind, importance=.1), 0, settings),
                                 Decision("motion", "low_importance"))
                self.assertEqual(self.agent.decide(event(kind=kind, importance=.65), 0, settings),
                                 Decision("speak", "important_event"))

    def test_companion_does_not_promote_uncertain_empty_or_unsafe_contexts(self):
        changes = (
            {"kind": "none", "importance": .9},
            {"kind": "sensitive", "importance": .9},
            {"kind": "private", "importance": .9},
            {"kind": "media", "confidence": .59},
        )
        for fields in changes:
            with self.subTest(fields=fields):
                self.assertEqual(self.agent.decide(event(**fields), 0, self.settings).action, "quiet")
        self.assertEqual(self.agent.decide(event(kind="media", reply="", importance=.1),
                                          0, self.settings), Decision("motion", "no_reply"))
        self.assertEqual(self.agent.decide(event(kind="media", importance=.09), 0, self.settings),
                         Decision("motion", "low_importance"))
        self.assertEqual(self.agent.decide(event(kind="work", importance=.1), 0, self.settings),
                         Decision("motion", "low_importance"))

    def test_companion_comments_still_obey_busy_idle_cooldown_and_hourly_limits(self):
        source = event(kind="media", importance=.1, confidence=.8)
        settings = replace(self.settings, max_speeches_per_hour=2)
        self.assertEqual(self.agent.decide(source, 0, settings, busy=True).reason, "user_busy")
        self.assertEqual(self.agent.decide(source, 0, settings, idle_seconds=600).reason, "user_idle")
        first = self.agent.decide(source, 0, settings)
        self.agent.record(first, 0)
        self.assertEqual(self.agent.decide(source, 179, settings), Decision("motion", "cooldown"))
        second = self.agent.decide(source, 180, settings)
        self.assertEqual(second.action, "speak")
        self.agent.record(second, 180)
        self.assertEqual(self.agent.decide(source, 360, settings), Decision("motion", "hourly_limit"))
        self.assertEqual(self.agent.decide(source, 3600, settings).action, "speak")

    def test_empty_reply_never_causes_an_empty_speech(self):
        self.assertEqual(self.agent.decide(event(reply=" "), 0, self.settings), Decision("motion", "no_reply"))

    def test_deciding_alone_does_not_consume_speech_budget(self):
        for _ in range(40):
            self.assertEqual(self.agent.decide(event(), 0, self.settings).action, "speak")
        self.agent.record(Decision("motion", "test"), 0)
        self.agent.record(Decision("quiet", "test"), 0)
        self.assertEqual(self.agent.decide(event(), 0, self.settings).action, "speak")

    def test_recorded_speech_enforces_cooldown_with_exact_boundary(self):
        decision = self.agent.decide(event(), 0, self.settings)
        self.agent.record(decision, 0)
        self.assertEqual(self.agent.decide(event(), 179, self.settings), Decision("motion", "cooldown"))
        self.assertEqual(self.agent.decide(event(), 180, self.settings).action, "speak")

    def test_hourly_limit_uses_rolling_window_and_expires(self):
        settings = replace(self.settings, max_speeches_per_hour=2, speech_cooldown_seconds=30)
        for now in (0, 30):
            decision = self.agent.decide(event(), now, settings)
            self.assertEqual(decision.action, "speak")
            self.agent.record(decision, now)
        self.assertEqual(self.agent.decide(event(), 60, settings), Decision("motion", "hourly_limit"))
        self.assertEqual(self.agent.decide(event(), 3600, settings).action, "speak")

    def test_private_or_empty_events_are_quiet(self):
        for kind in ("none", "no_change", "private", "sensitive"):
            with self.subTest(kind=kind):
                self.assertEqual(self.agent.decide(event(kind=kind), 0, self.settings).action, "quiet")

    def test_invalid_model_fields_are_safely_ignored(self):
        for changes in ({"confidence": float("nan")}, {"confidence": 1.1}, {"importance": -.1},
                        {"importance": "high"}, {"kind": []}, {"confidence": True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.agent.decide(event(**changes), 0, self.settings).action, "quiet")

    def test_reset_and_clock_rollback_release_budget(self):
        self.agent.record(Decision("speak", "test"), 100)
        self.assertEqual(self.agent.decide(event(), 101, self.settings).action, "motion")
        self.agent.reset()
        self.assertEqual(self.agent.decide(event(), 101, self.settings).action, "speak")
        self.agent.record(Decision("speak", "test"), 101)
        self.assertEqual(self.agent.decide(event(), 0, self.settings).action, "speak")

    def test_invalid_settings_or_clock_fall_back_to_quiet(self):
        self.assertEqual(self.agent.decide(event(), float("nan"), self.settings).reason, "invalid_clock")
        self.assertEqual(self.agent.decide(event(), 0, self.settings, idle_seconds=-1).reason, "invalid_clock")
        self.assertEqual(self.agent.decide(event(), 0, replace(self.settings, max_speeches_per_hour=0)).reason,
                         "invalid_settings")
        with self.assertRaises(ValueError):
            self.agent.record(Decision("speak", "test"), float("inf"))

    def test_event_contents_are_neither_executed_nor_mutated(self):
        source = event(reply="__import__('os').system('untrusted model text')")
        before = dict(vars(source))
        self.assertEqual(self.agent.decide(source, 0, self.settings).action, "speak")
        self.assertEqual(vars(source), before)


if __name__ == "__main__":
    unittest.main()

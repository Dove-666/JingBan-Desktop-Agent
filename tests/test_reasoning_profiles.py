"""Actual wire fields and capability identities; no inference or network calls."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop_pet.reasoning import (ReasoningError, _catalog, profile_options, reasoning_body,
    reasoning_identity, resolve_reasoning, validate_reasoning, validate_selection)


BASE = "https://relay.example/v1"


def metadata(model="aliased-model", **changes):
    value = {"version": 1, "base_url": BASE, "model": model, "profile": "openai", "values": ["low", "ultra"]}
    return {**value, **changes}


class ReasoningProfileTests(unittest.TestCase):
    def test_default_never_adds_reasoning_fields_in_any_profile(self):
        for profile, _ in profile_options():
            with self.subTest(profile=profile):
                self.assertEqual(reasoning_body(BASE, "model", profile=profile), {})

    def test_deepseek_off_and_real_levels_have_distinct_wire_values(self):
        cap = resolve_reasoning(BASE, "deepseek-flash")
        self.assertEqual(cap.values, ("default", "none", "low", "high", "max"))
        self.assertEqual(reasoning_body(BASE, "deepseek-flash", "none"), {"thinking": {"type": "disabled"}})
        for value in ("low", "high", "max"):
            self.assertEqual(reasoning_body(BASE, "deepseek-flash", value),
                {"thinking": {"type": "enabled"}, "reasoning_effort": value})
        with self.assertRaises(ReasoningError):
            reasoning_body(BASE, "deepseek-flash", "medium")

    def test_aggregator_protocol_precedes_underlying_model_family(self):
        base = "https://openrouter.ai/api/v1"
        model = "deepseek/deepseek-v4-pro"
        self.assertEqual(resolve_reasoning(base, model).values, ("default",))
        declared = metadata(model=model, base_url=base, profile="openrouter",
            values=["none", "minimal", "low", "medium", "high", "xhigh"])
        cap = resolve_reasoning(base, model, metadata=declared)
        self.assertEqual(cap.id, "openrouter")
        self.assertIn("xhigh", cap.values)
        self.assertNotIn("max", cap.values)
        self.assertEqual(reasoning_body(base, model, "high", metadata=declared), {"reasoning": {"effort": "high"}})
        self.assertEqual(reasoning_body(base, model, "none", metadata=declared), {"reasoning": {"enabled": False}})

    def test_provider_host_does_not_claim_every_model_supports_thinking(self):
        for base in ("https://openrouter.ai/api/v1", "https://api.siliconflow.cn/v1", "https://api-inference.modelscope.cn/v1"):
            for model in ("unverified-alias", "deepseek-flash", "gpt-5.4"):
                self.assertEqual(resolve_reasoning(base, model).values, ("default",))
        self.assertEqual(resolve_reasoning(BASE, "gpt-4o").id, "omit")
        declared = metadata(model="gpt-4o", values=["high"])
        self.assertEqual(resolve_reasoning(BASE, "gpt-4o", metadata=declared).source, "metadata")

    def test_openai_presets_are_exact_model_or_date_snapshot_only(self):
        for model in ("gpt-5", "gpt-5-2025-08-07", "openai/gpt-5-2025-08-07"):
            cap = resolve_reasoning(BASE, model)
            self.assertEqual(cap.values, ("default", "minimal", "low", "medium", "high"))
            self.assertFalse(cap.allow_custom)
        for model in ("gpt-5-pro", "gpt-5-mini", "gpt-5-codex", "gpt-5-2025-08-07-extra", "gpt-5.1-pro"):
            self.assertEqual(resolve_reasoning(BASE, model).values, ("default",))
        self.assertNotIn("minimal", resolve_reasoning(BASE, "gpt-5.1").values)
        self.assertIn("none", resolve_reasoning(BASE, "gpt-5.1").values)
        self.assertIn("xhigh", resolve_reasoning(BASE, "gpt-5.4").values)
        self.assertNotIn("max", resolve_reasoning(BASE, "gpt-5.4").values)
        self.assertNotIn("none", resolve_reasoning(BASE, "gpt-6-astra").values)
        self.assertIn("none", resolve_reasoning(BASE, "gpt-6-luna").values)

    def test_native_claude_never_inferred_from_model_name(self):
        cap = resolve_reasoning(BASE, "claude-sonnet-4-6")
        self.assertEqual(cap.values, ("default",))
        self.assertEqual(reasoning_body(BASE, "claude-sonnet-4-6", "high", "claude_adaptive"),
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}})

    def test_gemini_compatibility_depends_on_endpoint_and_specific_model(self):
        google = "https://generativelanguage.googleapis.com/v1beta/openai"
        pro = resolve_reasoning(google, "gemini-3.1-pro-preview")
        self.assertEqual(pro.values, ("default", "low", "medium", "high"))
        self.assertNotIn("none", resolve_reasoning(google, "gemini-2.5-pro").values)
        self.assertIn("none", resolve_reasoning(google, "gemini-2.5-flash").values)
        self.assertIn("minimal", resolve_reasoning(google, "gemini-3-flash-preview").values)
        self.assertEqual(resolve_reasoning("https://generativelanguage.googleapis.com/v1beta", "gemini-3-flash-preview").values, ("default",))
        self.assertEqual(resolve_reasoning(BASE, "gemini-3-flash-preview").values, ("default",))
        self.assertEqual(resolve_reasoning(google, "gemini-3-flash-image").values, ("default",))

    def test_explicit_toggle_and_budget_modes_use_their_actual_fields(self):
        self.assertEqual(reasoning_body(BASE, "m", "enabled", "thinking"), {"thinking": {"type": "enabled"}})
        self.assertEqual(reasoning_body(BASE, "m", "none", "enable_thinking"), {"enable_thinking": False})
        self.assertEqual(reasoning_body(BASE, "m", "enabled", "qwen_budget", 12345),
            {"enable_thinking": True, "thinking_budget": 12345})
        self.assertEqual(reasoning_body(BASE, "m", "enabled", "claude_budget", 2048),
            {"thinking": {"type": "enabled", "budget_tokens": 2048}})
        self.assertEqual(reasoning_body(BASE, "m", "none", "claude_budget", 2048), {"thinking": {"type": "disabled"}})

    def test_budget_zero_uses_declared_default_and_invalid_range_is_rejected(self):
        self.assertEqual(reasoning_body(BASE, "m", "enabled", "claude_budget"),
            {"thinking": {"type": "enabled", "budget_tokens": 4096}})
        for bad in (-1, 1, 1023, 262145, True, 1.0, "2048"):
            with self.subTest(bad=bad), self.assertRaises(ReasoningError):
                reasoning_body(BASE, "m", "enabled", "claude_budget", bad)
        with self.assertRaises(ReasoningError):
            reasoning_body(BASE, "m", "high", "legacy", 2048)

    def test_unknown_auto_is_conservative_and_explicit_custom_retains_new_token(self):
        self.assertEqual(resolve_reasoning(BASE, "renamed-unknown").values, ("default",))
        with self.assertRaises(ReasoningError):
            reasoning_body(BASE, "renamed-unknown", "ultra")
        self.assertEqual(reasoning_body(BASE, "renamed-unknown", "ultra", "custom"), {"reasoning_effort": "ultra"})
        self.assertEqual(reasoning_body(BASE, "renamed-unknown", "Ultra-Deep_2", "custom"), {"reasoning_effort": "Ultra-Deep_2"})

    def test_legacy_old_enum_is_never_automatically_mapped(self):
        for value in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            self.assertEqual(reasoning_body(BASE, "deepseek-flash", value, "legacy"), {"reasoning_effort": value})
        with self.assertRaises(ReasoningError):
            validate_reasoning("legacy", "ultra")

    def test_custom_tokens_are_bounded_and_error_text_does_not_echo_input(self):
        for value in ("", "x" * 65, "high\n", 'high"', {"effort": "high"}, None, True):
            with self.subTest(value=value), self.assertRaises(ReasoningError) as raised:
                reasoning_body(BASE, "m", value, "custom")
            self.assertEqual(str(raised.exception), "当前模型或接口不支持所选思考设置，请选择可用选项或显式自定义协议。")

    def test_bound_metadata_can_declare_model_specific_tokens(self):
        cap = resolve_reasoning(BASE, "aliased-model", metadata=metadata())
        self.assertEqual(cap.source, "metadata")
        self.assertEqual(cap.values, ("default", "low", "ultra"))
        self.assertFalse(cap.allow_custom)
        self.assertEqual(reasoning_body(BASE, "aliased-model", "ultra", metadata=metadata()), {"reasoning_effort": "ultra"})

    def test_metadata_must_match_complete_api_identity_and_exact_model(self):
        invalid = (metadata(base_url="https://other.example/v1"), metadata(base_url="https://relay.example/v2"),
            metadata(base_url="http://relay.example/v1"), metadata(base_url="https://relay.example:444/v1"),
            metadata(model="ALIASED-model"), metadata(version=True), metadata(profile="legacy"))
        for value in invalid:
            self.assertEqual(resolve_reasoning(BASE, "aliased-model", metadata=value).values, ("default",))
        self.assertEqual(resolve_reasoning("https://RELAY.example:443/v1/", "aliased-model", metadata=metadata()).source, "metadata")
        self.assertNotEqual(reasoning_identity(BASE, "m"), reasoning_identity("https://relay.example:0/v1", "m"))

    def test_external_metadata_cannot_inject_arbitrary_request_fields(self):
        for value in (metadata(extra_body={"messages": ["evil"]}), metadata(api_key="secret"),
                      metadata(values=["high", "bad token"]), metadata(profile="custom"),
                      metadata(profile="thinking", values=["ultra"])):
            self.assertEqual(resolve_reasoning(BASE, "aliased-model", metadata=value).source, "unknown")
            self.assertEqual(reasoning_body(BASE, "aliased-model", metadata=value), {})

    def test_temperature_policy_is_shared_without_adding_default_body_fields(self):
        self.assertEqual(resolve_reasoning(BASE, "gpt-5").temperature_policy, "omit")
        self.assertEqual(resolve_reasoning(BASE, "deepseek-flash").temperature_policy, "omit")
        self.assertEqual(resolve_reasoning(BASE, "gpt-5", "legacy").temperature_policy, "preserve")
        self.assertEqual(reasoning_body(BASE, "gpt-5"), {})
        declared = metadata(model="gpt-5", values=["low"])
        self.assertEqual(resolve_reasoning(BASE, "gpt-5", metadata=declared).temperature_policy, "omit")

    def test_capability_and_json_view_cannot_mutate_the_catalog(self):
        cap = resolve_reasoning(BASE, "deepseek-flash")
        with self.assertRaises(FrozenInstanceError):
            cap.id = "custom"
        view = cap.to_dict()
        view["values"].append("evil")
        self.assertNotIn("evil", resolve_reasoning(BASE, "deepseek-flash").values)
        self.assertEqual(cap.label_for("high"), "高")

    def test_catalog_is_packaged_at_the_shared_runtime_resource_location(self):
        _catalog.cache_clear()
        try:
            with tempfile.TemporaryDirectory() as folder, patch("desktop_pet.reasoning.resource_root", return_value=Path(folder)):
                with self.assertRaises(ReasoningError):
                    resolve_reasoning(BASE, "m")
                path = Path(folder) / "shared/reasoning/profiles.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"version": 1, "profiles": {}}), encoding="utf-8")
                with self.assertRaises(ReasoningError):
                    resolve_reasoning(BASE, "m")
        finally:
            _catalog.cache_clear()


if __name__ == "__main__":
    unittest.main()

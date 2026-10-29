"""Opt-in visible Qt animation/CPU smoke test, without moving the mouse.

Run: .venv\\Scripts\\python.exe tests\\animation_smoke.py --seconds 12
Only this process's visible pet is captured; the desktop is never captured.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.pet_window import PetWindow
from desktop_pet.windows_work_area import work_area


def run_validation(seconds: float = 12.0) -> int:
    if not math.isfinite(seconds) or seconds < 5:
        raise ValueError("Sample duration must be finite and at least 5 seconds")
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    report = {"status": "failed", "requested_sample_seconds": seconds}
    pet = None
    sampling = False
    states_seen = set()
    sample = {
        "timer_frame_count": 0,
        "offset_y_min": math.inf,
        "offset_y_max": -math.inf,
        "scale_min": math.inf,
        "scale_max": -math.inf,
        "window_position_stable": True,
        "source_cache_key_stable": True,
        "scaled_sprite_cache_key_stable": True,
        "hit_image_cache_key_stable": True,
        "sprite_within_drawing_area": True,
    }
    exit_code = 1
    try:
        pet = PetWindow(PetConfig())
        # Keep this probe away from the normal bottom-right pet/input probe.
        area = work_area(app.primaryScreen())
        pet.move(area.left() + 24, area.center().y() - pet.height() // 2)
        pet.show()
        initial_position = pet.pos()
        source_key = pet._source.cacheKey()
        sprite_key = pet._sprite.cacheKey()
        hit_key = pet._hit_image.cacheKey()

        def observe_frame(_dt: float) -> None:
            states_seen.add(pet.animation.state.value)
            if not sampling:
                return
            pose = pet.animation.pose
            sample["timer_frame_count"] += 1
            sample["offset_y_min"] = min(sample["offset_y_min"], pose.offset_y)
            sample["offset_y_max"] = max(sample["offset_y_max"], pose.offset_y)
            sample["scale_min"] = min(sample["scale_min"], pose.scale)
            sample["scale_max"] = max(sample["scale_max"], pose.scale)
            sample["window_position_stable"] &= pet.pos() == initial_position
            sample["source_cache_key_stable"] &= pet._source.cacheKey() == source_key
            sample["scaled_sprite_cache_key_stable"] &= pet._sprite.cacheKey() == sprite_key
            sample["hit_image_cache_key_stable"] &= pet._hit_image.cacheKey() == hit_key
            sample["sprite_within_drawing_area"] &= pet._drawing_area.adjusted(
                -1e-8, -1e-8, 1e-8, 1e-8,
            ).contains(pet.animated_sprite_rect)

        callback_errors = []

        def checked(callback):
            # Qt otherwise prints callback exceptions and keeps its loop alive.
            def invoke(*args):
                try:
                    callback(*args)
                except Exception as exc:
                    callback_errors.append(exc)
                    app.quit()
            return invoke

        start_cpu = start_wall = elapsed_cpu = elapsed_wall = 0.0

        def finish_sampling() -> None:
            nonlocal sampling, elapsed_cpu, elapsed_wall
            sampling = False
            elapsed_wall = time.perf_counter() - start_wall
            elapsed_cpu = time.process_time() - start_cpu
            verify_after_sampling()
            app.quit()

        sample_end_timer = QTimer(pet)
        sample_end_timer.setSingleShot(True)
        sample_end_timer.setTimerType(Qt.TimerType.PreciseTimer)
        sample_end_timer.timeout.connect(checked(finish_sampling))

        def begin_sampling() -> None:
            nonlocal sampling, start_cpu, start_wall
            start_cpu = time.process_time()
            start_wall = time.perf_counter()
            sampling = True
            sample_end_timer.start(round(seconds * 1000))

        def verify_after_sampling() -> None:
            assert elapsed_wall > 0, "Sampling ended before its measurement timer"
            sample.update({
                "sampling_event_loop": "QApplication.exec",
                "warmup_seconds": 1.0,
                "actual_sample_seconds": elapsed_wall,
                "process_cpu_seconds": elapsed_cpu,
                "process_cpu_single_core_percent": 100.0 * elapsed_cpu / elapsed_wall,
                "logical_cpu_count": os.cpu_count(),
                "process_cpu_machine_capacity_percent": (
                    100.0 * elapsed_cpu / elapsed_wall / (os.cpu_count() or 1)
                ),
                "timer_fps": sample["timer_frame_count"] / elapsed_wall,
                "configured_fps": pet.config.refresh_fps,
                "window_position": [initial_position.x(), initial_position.y()],
                "source_cache_key": source_key,
            })
            report["idle_sample"] = sample
            assert sample["timer_frame_count"] > seconds * pet.config.refresh_fps * 0.5
            assert sample["offset_y_max"] - sample["offset_y_min"] > 1.0
            assert sample["scale_max"] - sample["scale_min"] > 0.002
            for field in (
                "window_position_stable", "source_cache_key_stable",
                "scaled_sprite_cache_key_stable", "hit_image_cache_key_stable",
                "sprite_within_drawing_area",
            ):
                assert sample[field], field
            verify_actions()

        def wait_for_timers(milliseconds: int) -> None:
            # A local event loop preserves the application and its visible pet.
            # QApplication.quit closes windows, so it is reserved for final exit.
            loop = QEventLoop()
            wait_timer = QTimer(loop)
            wait_timer.setSingleShot(True)
            wait_timer.setTimerType(Qt.TimerType.PreciseTimer)
            wait_timer.timeout.connect(loop.quit)
            wait_timer.start(milliseconds)
            loop.exec()
            if callback_errors:
                raise callback_errors[0]
            wait_timer.deleteLater()

        def verify_actions() -> None:
            action_checks = {}
            for name, play, duration in (
                ("happy", pet.animation.play_happy, pet.config.animation.happy_duration),
                ("surprised", pet.animation.play_surprised, pet.config.animation.surprised_duration),
            ):
                start_pose = pet.animation.pose
                assert play(), f"{name} trigger rejected"
                assert pet.animation.state.value == name, f"{name} state did not start"
                assert pet.animation.pose == start_pose, f"{name} changed pose at trigger"
                wait_for_timers(round(duration * pet.config.animation.jump_rise_fraction * 1000) + 20)
                peak_pose = pet.animation.pose
                assert peak_pose.offset_y < start_pose.offset_y, f"{name} did not jump upward"
                wait_for_timers(round(duration * 1000) + 100)
                assert pet.animation.state == AnimationState.IDLE, f"{name} did not return to idle"
                action_checks[name] = {
                    "continuous_start": True,
                    "start_offset_y": start_pose.offset_y,
                    "observed_jump_offset_y": peak_pose.offset_y,
                    "returned_to_idle_by_real_timer": True,
                }

            pet.animation.start_dragging()
            frozen_pose = pet.animation.pose
            wait_for_timers(350)
            assert pet.animation.state == AnimationState.DRAGGING
            assert pet.animation.pose == frozen_pose
            assert not pet.animation.play_happy()
            assert not pet.animation.play_surprised()
            pet.animation.finish_dragging()
            assert pet.animation.state == AnimationState.RETURNING
            assert pet.animation.pose == frozen_pose
            wait_for_timers(round(pet.config.animation.return_duration * 1000) + 120)
            assert pet.animation.state == AnimationState.IDLE
            report["actions"] = action_checks
            report["dragging"] = {
                "frozen_during_real_timer_ticks": True,
                "actions_rejected_while_dragging": True,
                "continuous_release": True,
                "returned_to_idle_by_real_timer": True,
            }
            report["observed_states"] = sorted(states_seen)
            assert states_seen == {state.value for state in AnimationState}
            preview_path = artifacts / "animation_preview.png"
            assert pet.grab().save(str(preview_path))
            report["preview"] = str(preview_path.relative_to(ROOT))
            report["status"] = "passed"

        pet.frame_tick.connect(checked(observe_frame))
        QTimer.singleShot(1000, checked(begin_sampling))
        # CPU/frame sampling uses this normal application main loop. Action
        # checks run afterward, before QApplication.quit closes the window.
        app.exec()
        if callback_errors:
            raise callback_errors[0]
        assert report["status"] == "passed", "Validation ended before all stages completed"
        exit_code = 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if pet is not None:
            pet.close()
            pet.deleteLater()
            app.processEvents()
        app.quit()
        report_path = artifacts / "animation_smoke.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


def _sample_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 5:
        raise argparse.ArgumentTypeError("--seconds must be finite and at least 5")
    return seconds


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=_sample_seconds, default=12.0)
    arguments = parser.parse_args()
    raise SystemExit(run_validation(arguments.seconds))

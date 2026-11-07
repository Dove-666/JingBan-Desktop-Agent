"""Offline metadata/probe contracts; no production configuration or documents."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from desktop_pet.agents import health
from tools import agent_doctor


PRIVATE = "Authorization: Bearer sk-SYNTHETIC_SECRET C:\\Users\\PrivatePerson\\RealDocumentTitle.docx"


class FakeDependencies(health.HealthDependencies):
    def __init__(self):
        self.calls = []
        self.environment = []
        self.failed = None
        self.bad_version = None
        self.windows = {"supported": True, "session_active": True, "input_default": True,
                        "monitor_count": 1, "dpi_awareness": 2}

    def run(self, kind, command, **options):
        self.calls.append(kind)
        self.environment.append(options["environment"])
        if self.failed == kind:
            raise RuntimeError(PRIVATE)
        if kind == "python_metadata":
            data = {"python": "3.12.13", "packages": {name: {"present": True, "version": self.bad_version or "1.2.3"}
                    for name in health._DEPENDENCIES}}
        elif kind == "node_version":
            return health.CommandResult("OK", b"v24.1.0\n", 0)
        else:
            data = {"passed": True, "ignored_exception": PRIVATE}
        return health.CommandResult("OK", json.dumps(data).encode(), 0)

    def path_metadata(self, value, **_):
        return {"selected": value is not None, "local": value is not None, "exists": value is not None,
                "readable": value is not None, "writable": value is not None}

    def ui_tars_metadata(self, root):
        return {"sdk": "1.2.3", "operator-nut-js": "1.2.3"}

    def windows_metadata(self):
        return self.windows


def request(**options):
    return health.DoctorRequest(backend_python=sys.executable, node_path=sys.executable,
        workspace=Path.cwd(), **options)


class AgentHealthMetadataTests(unittest.TestCase):
    def test_import_does_not_start_process_thread_probe_or_observe_windows(self):
        with patch("subprocess.Popen", side_effect=AssertionError("No process on import")), \
             patch("threading.Thread.start", side_effect=AssertionError("No thread on import")), \
             patch.object(health.HealthDependencies, "windows_metadata", side_effect=AssertionError("No desktop query on import")), \
             patch("pathlib.Path.open", side_effect=AssertionError("No user file on import")):
            importlib.reload(health)

    def test_metadata_is_not_runtime_ready_and_never_loads_sdk_or_creates_temp_fixture(self):
        fake = FakeDependencies()
        with patch.object(health.tempfile, "TemporaryDirectory", side_effect=AssertionError("No fixture in metadata mode")):
            report = health.collect_agent_health(request(gui_configured=True), dependencies=fake)
        self.assertEqual(fake.calls, ["python_metadata", "node_version"])
        self.assertEqual(report.overall, "metadata_only")
        self.assertEqual(report.mode, "metadata")
        self.assertTrue(all(item["runtime_ready"] is None for item in report.capabilities))
        self.assertTrue(any(item["state"] == "unverified" for item in report.capabilities))
        self.assertFalse(any((report.network_requests, report.model_requests, report.screenshots_captured,
                              report.physical_input_executed, report.production_files_modified)))
        self.assertEqual(next(row for row in report.checks if row.id == "nanobot").status, "unverified")

    def test_explicit_probes_report_only_their_verified_scope_and_keep_actual_tasks_unverified(self):
        fake = FakeDependencies()
        report = health.collect_agent_health(request(probe=True, gui_configured=True), dependencies=fake)
        self.assertEqual(report.overall, "probe_passed")
        self.assertEqual(fake.calls, ["python_metadata", "node_version", "sdk_probe", "vm_probe", "document_probe", "ui_tars_probe"])
        evidence = next(row for row in report.checks if row.id == "sdk_probe").evidence
        self.assertTrue(evidence["sdk_read_fixture_verified"])
        self.assertFalse(evidence["asyncio_runner_verified"])
        self.assertFalse(evidence["model_loop_verified"])
        self.assertTrue(all(item["runtime_ready"] is None for item in report.capabilities))
        self.assertNotIn(PRIVATE, json.dumps(report.to_dict()))

    def test_missing_dependencies_and_inactive_desktop_get_fixed_actionable_codes(self):
        fake = FakeDependencies(); fake.failed = "python_metadata"
        fake.windows = {"supported": True, "session_active": False, "input_default": False, "monitor_count": 2, "dpi_awareness": 0}
        report = health.collect_agent_health(request(), dependencies=fake)
        codes = {check.code for check in report.checks}
        self.assertIn("PYTHON_START_FAILED", codes)
        self.assertIn("NANOBOT_MISSING", codes)
        self.assertIn("WINDOWS_SESSION_UNAVAILABLE", codes)
        self.assertIn("INPUT_DESKTOP_UNAVAILABLE", codes)
        self.assertIn("MULTI_DISPLAY_UNSUPPORTED", codes)
        self.assertEqual(report.overall, "blocked")
        self.assertNotIn("PrivatePerson", json.dumps(report.to_dict()))
        self.assertTrue(all(check.recommendation for check in report.checks if check.status == "blocked"))

    def test_event_loop_outside_probe_scope_is_unverified_instead_of_broken_sdk(self):
        class AsyncFixture(FakeDependencies):
            def run(self, kind, *args, **kwargs):
                if kind == "sdk_probe":
                    return health.CommandResult("OK", b'{"passed":false,"scope_unverified":true}', 0)
                return super().run(kind, *args, **kwargs)
        report = health.collect_agent_health(request(probe=True), dependencies=AsyncFixture())
        sdk = next(check for check in report.checks if check.id == "sdk_probe")
        self.assertEqual(sdk.status, "unverified")
        self.assertEqual(sdk.code, "SDK_PROBE_ASYNC_UNVERIFIED")
        self.assertEqual(report.overall, "probe_partial")

    def test_credentials_paths_titles_and_arbitrary_version_text_are_never_exported(self):
        fake = FakeDependencies(); fake.bad_version = PRIVATE
        with patch.dict("os.environ", {"OPENAI_API_KEY": PRIVATE, "HTTP_PROXY": PRIVATE, "AURA_GUI_KEY": PRIVATE}):
            report = health.collect_agent_health(request(database=PRIVATE), dependencies=fake)
        rendered = json.dumps(report.to_dict()) + repr(request(database=PRIVATE))
        for private in (PRIVATE, "PrivatePerson", "RealDocumentTitle", "SYNTHETIC_SECRET", "Authorization", "Bearer"):
            self.assertNotIn(private, rendered)
            self.assertFalse(any(private in str(env) for env in fake.environment))

    def test_unknown_dependency_failure_codes_and_payloads_cannot_escape(self):
        class Poisoned(FakeDependencies):
            def run(self, *args, **kwargs):
                return health.CommandResult(PRIVATE, PRIVATE.encode(), 0)
        report = health.collect_agent_health(request(), dependencies=Poisoned())
        self.assertIn("PYTHON_START_FAILED", {row.code for row in report.checks})
        self.assertNotIn(PRIVATE, json.dumps(report.to_dict()))

    def test_successful_process_with_invalid_metadata_is_never_reported_as_ok(self):
        class Invalid(FakeDependencies):
            def run(self, *args, **kwargs):
                return health.CommandResult("OK", PRIVATE.encode(), 0)
        report = health.collect_agent_health(request(), dependencies=Invalid())
        codes = {row.code for row in report.checks}
        self.assertIn("PYTHON_METADATA_INVALID", codes)
        self.assertIn("NODE_VERSION_INVALID", codes)
        self.assertNotIn(PRIVATE, json.dumps(report.to_dict()))

    def test_pre_cancelled_request_performs_no_io_or_command(self):
        event = threading.Event(); event.set()
        fake = FakeDependencies()
        with patch.object(health.shutil, "which", side_effect=AssertionError("No discovery after cancellation")):
            report = health.collect_agent_health(health.DoctorRequest(probe=True, cancel_event=event), dependencies=fake)
        self.assertEqual(report.overall, "cancelled"); self.assertEqual(fake.calls, [])

    def test_disabled_permissions_stay_disabled_without_loading_credentials(self):
        report = health.collect_agent_health(request(tools_enabled=False, computer_enabled=False), dependencies=FakeDependencies())
        self.assertTrue(all(not item["declared_enabled"] and item["state"] == "disabled" for item in report.capabilities))

    def test_invalid_request_raises_only_fixed_error(self):
        with self.assertRaisesRegex(ValueError, "^DOCTOR_INVALID_REQUEST$"):
            health.collect_agent_health(False)
        for value in (0, -1, float("nan"), True, 121):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^DOCTOR_INVALID_REQUEST$"):
                health.collect_agent_health(request(timeout_seconds=value))
        self.assertIsNone(health._local_path(r"\\private-host\secret-share\private.db"))


class AgentHealthProcessTests(unittest.TestCase):
    def run_child(self, script, *, timeout=2, event=None, cwd=None):
        return health.HealthDependencies().run("controlled_test", [sys.executable, "-I", "-B", "-c", script],
            timeout=timeout, cancel_event=event or threading.Event(), cwd=cwd or str(Path.cwd()), environment=health._environment(cwd))

    def test_actual_timeout_kills_only_controlled_child(self):
        started = time.monotonic()
        result = self.run_child("import time; time.sleep(10)", timeout=.1)
        self.assertEqual(result.code, "TIMEOUT"); self.assertLess(time.monotonic() - started, 4)

    def test_actual_cancellation_interrupts_controlled_child_without_returning_partial_raw_stdout(self):
        event = threading.Event(); timer = threading.Timer(.12, event.set); timer.start()
        try:
            result = self.run_child("import time; print('Authorization Bearer synthetic',flush=True); time.sleep(10)", event=event)
        finally:
            timer.cancel(); timer.join()
        self.assertEqual(result.code, "CANCELLED"); self.assertEqual(result.stdout, b"")

    def test_actual_output_limit_and_stderr_suppression(self):
        result = self.run_child("print('x'*70000)")
        self.assertEqual(result.code, "OUTPUT_LIMIT")
        result = self.run_child("import sys; sys.stderr.write('Authorization Bearer synthetic'); print('{\"passed\":true}')")
        self.assertEqual(result.code, "OK"); self.assertNotIn(b"Authorization", result.stdout)

    def test_child_exit_with_descendant_holding_stdout_cannot_block_drain(self):
        started = time.monotonic()
        script = "import subprocess,sys; subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(10)'],stdout=sys.stdout,stderr=sys.stderr); print('controlled-parent-finished')"
        result = self.run_child(script)
        self.assertEqual(result.code, "OK")
        self.assertIn(b"controlled-parent-finished", result.stdout)
        self.assertLess(time.monotonic() - started, 4)

    def test_probe_audit_blocks_network_and_outside_fixture_writes(self):
        with tempfile.TemporaryDirectory(prefix="aura-doctor-test-") as directory:
            fixture = Path(directory) / "probe"; fixture.mkdir()
            for body in ("import socket\nsocket.create_connection(('example.invalid',443))",
                         "(root.parent / 'outside.txt').write_text('must-not-write')"):
                result = self.run_child(health._probe_script(body), cwd=str(fixture))
                self.assertEqual(result.code, "OK")
                self.assertFalse(json.loads(result.stdout)["passed"])
            self.assertFalse((Path(directory) / "outside.txt").exists())


class AgentDoctorCliTests(unittest.TestCase):
    def test_json_output_is_parseable_and_safe_and_help_does_not_list_private_defaults(self):
        report = health.HealthReport(1, "metadata", "metadata_only", 1, (), ())
        output = io.StringIO()
        with patch.object(health, "collect_agent_health", return_value=report), redirect_stdout(output):
            self.assertEqual(agent_doctor.main(["--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["overall"], "metadata_only")
        self.assertNotIn("Users", output.getvalue())

    def test_invalid_cli_arguments_and_internal_exceptions_cannot_echo_private_input(self):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit):
            agent_doctor.main(["--unknown", PRIVATE])
        self.assertNotIn(PRIVATE, output.getvalue())
        output = io.StringIO()
        with patch.object(health, "collect_agent_health", side_effect=RuntimeError(PRIVATE)), redirect_stdout(output):
            self.assertEqual(agent_doctor.main(["--json"]), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "DOCTOR_INVALID_REQUEST")
        self.assertNotIn(PRIVATE, output.getvalue())


if __name__ == "__main__":
    unittest.main()

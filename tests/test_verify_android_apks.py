"""Release checks reject incomplete identity and corrupt non-native entries."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from tools import verify_android_apks as verifier


class AndroidApkVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.delivery = self.root / "android/deliverables"
        self.delivery.mkdir(parents=True)
        for architecture in ("arm64-v8a", "armeabi-v7a", "x86_64", "universal"):
            path = self.delivery / f"AURA-1.14.3-{architecture}.apk"
            abis = ("arm64-v8a", "armeabi-v7a", "x86_64") if architecture == "universal" else (architecture,)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("assets/payload.txt", b"RELEASE_CRC_SENTINEL")
                for abi in abis:
                    for library in ("libiroh_ffi.so", "libjnidispatch.so"):
                        archive.writestr(f"lib/{abi}/{library}", b"owned-native-fixture")
        self.baseline = self.root / "artifacts/android/qa/apks/AURA-1.0.0-arm64-v8a-signature.txt"
        self.baseline.parent.mkdir(parents=True)
        self.digest = "a" * 64
        self.baseline.write_text(f"Signer #1 certificate SHA-256 digest: {self.digest}\n")
        tests = self.root / "android/app/build/test-results/testDebugUnitTest"
        tests.mkdir(parents=True)
        (tests / "TEST-owned.xml").write_text('<testsuite name="owned" tests="1" failures="0" errors="0" skipped="0"/>')
        self.code = 20
        self.extra_badging = ""
        self.report_path = self.root / "artifacts/android/qa/apk-verification-1.14.3.json"
        self.sdk_calls = []

    def command_output(self, command, environment):
        self.sdk_calls.append(command)
        if "verify" in command:
            return ("Verified using v2 scheme (APK Signature Scheme v2): true\n"
                    f"Signer #1 certificate SHA-256 digest: {self.digest}\n")
        if "badging" in command:
            return (f"package: name='com.aura.companion' versionCode='{self.code}' versionName='1.14.3'\n"
                    "minSdkVersion:'26'\ntargetSdkVersion:'35'\n" + self.extra_badging)
        return ""

    def run_verifier(self, explicit_code=True):
        arguments = ["verify_android_apks.py", "--version", "1.14.3"]
        if explicit_code:
            arguments.extend(["--version-code", "20"])
        with patch.object(verifier, "ROOT", self.root), patch.object(verifier, "run", self.command_output), \
                patch.object(verifier, "elf_load_segments", return_value=[{"alignment": 16384}]), \
                patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
            verifier.main()

    def test_valid_release_with_explicit_version_code(self):
        self.run_verifier()
        self.assertTrue((self.root / "artifacts/android/qa/apk-verification-1.14.3.json").is_file())

    def test_existing_version_only_invocation_remains_supported(self):
        self.run_verifier(explicit_code=False)

    def test_renamed_release_retains_package_identity_and_checks_display_label(self):
        for package in self.delivery.glob("AURA-*.apk"):
            package.rename(package.with_name(package.name.replace("AURA-", "鲸伴-", 1)))
        self.extra_badging = "application-label:'鲸伴'\n"
        self.run_verifier()
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertTrue(all(Path(row["file"]).name.startswith("鲸伴-") for row in report["packages"]))

    def test_renaming_only_apk_filename_does_not_pass_brand_verification(self):
        for package in self.delivery.glob("AURA-*.apk"):
            package.rename(package.with_name(package.name.replace("AURA-", "鲸伴-", 1)))
        self.extra_badging = "application-label:'AURA'\n"
        with self.assertRaisesRegex(RuntimeError, "application label"):
            self.run_verifier()

    def test_ambiguous_product_releases_require_an_explicit_selection(self):
        for package in self.delivery.glob("AURA-*.apk"):
            package.with_name(package.name.replace("AURA-", "鲸伴-", 1)).write_bytes(package.read_bytes())
        with self.assertRaisesRegex(RuntimeError, "Multiple product names"):
            self.run_verifier()
        product, selected = verifier._release_packages(self.delivery, "1.14.3", "AURA")
        self.assertEqual(product, "AURA")
        self.assertEqual(len(selected), 4)

    def test_update_requires_original_signer_evidence(self):
        self.baseline.unlink()
        with self.assertRaisesRegex(RuntimeError, "signing certificate"):
            self.run_verifier(explicit_code=False)

    def test_update_rejects_different_original_signer(self):
        self.baseline.write_text(f"Signer #1 certificate SHA-256 digest: {'b' * 64}\n")
        with self.assertRaisesRegex(RuntimeError, "signing certificate"):
            self.run_verifier(explicit_code=False)

    def test_update_rejects_unreadable_original_signer(self):
        self.baseline.write_text("signature evidence unavailable\n")
        with self.assertRaisesRegex(RuntimeError, "signing certificate"):
            self.run_verifier(explicit_code=False)

    def test_wrong_version_code_is_rejected(self):
        self.code = 19
        with self.assertRaisesRegex(RuntimeError, "versionCode"):
            self.run_verifier()

    def test_label_cannot_supply_package_version_code(self):
        self.code = 19
        self.extra_badging = "application-label:'versionCode='20''\n"
        with self.assertRaisesRegex(RuntimeError, "versionCode"):
            self.run_verifier()

    def test_corrupt_non_native_entry_is_rejected(self):
        path = self.delivery / "AURA-1.14.3-arm64-v8a.apk"
        payload = path.read_bytes()
        self.assertEqual(payload.count(b"RELEASE_CRC_SENTINEL"), 1)
        path.write_bytes(payload.replace(b"RELEASE_CRC_SENTINEL", b"RELEASE_CRC_SENTINE!"))
        with self.assertRaisesRegex(RuntimeError, "ZIP"):
            self.run_verifier(explicit_code=False)

    def test_failed_rerun_archives_success_and_publishes_failed_attempt(self):
        self.run_verifier()
        original = self.report_path.read_bytes()
        self.baseline.unlink()
        with self.assertRaisesRegex(RuntimeError, "signing certificate"):
            self.run_verifier()
        failed = json.loads(self.report_path.read_bytes())
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["run_id"])
        self.assertEqual(failed["packages"], [])
        self.assertEqual(failed["unit_tests"], [])
        self.assertEqual((self.root / failed["previous_report"]).read_bytes(), original)

    def test_legacy_success_is_archived_byte_for_byte_before_new_success(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\r\n'
        self.report_path.write_bytes(original)
        self.run_verifier()
        passed = json.loads(self.report_path.read_bytes())
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(len(passed["packages"]), 4)
        self.assertEqual(passed["unit_tests_total"], 1)
        self.assertEqual((self.root / passed["previous_report"]).read_bytes(), original)

    def test_first_failed_attempt_leaves_an_explicit_failed_report(self):
        self.baseline.unlink()
        with self.assertRaisesRegex(RuntimeError, "signing certificate"):
            self.run_verifier()
        failed = json.loads(self.report_path.read_bytes())
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["run_id"])
        self.assertIsNone(failed["previous_report"])

    def test_success_after_failure_preserves_the_failed_attempt(self):
        self.baseline.unlink()
        with self.assertRaisesRegex(RuntimeError, "signing certificate"):
            self.run_verifier()
        original = self.report_path.read_bytes()
        self.baseline.write_text(f"Signer #1 certificate SHA-256 digest: {self.digest}\n")
        self.run_verifier()
        passed = json.loads(self.report_path.read_bytes())
        self.assertEqual(passed["status"], "passed")
        self.assertNotEqual(passed["run_id"], json.loads(original)["run_id"])
        self.assertEqual((self.root / passed["previous_report"]).read_bytes(), original)

    def test_success_stdout_retains_legacy_fields(self):
        with patch("builtins.print") as emit:
            self.run_verifier()
        emit.assert_called_once()
        self.assertEqual(json.loads(emit.call_args.args[0]), {
            "verified_apks": 4, "unit_tests": 1, "report": str(self.report_path)})

    def test_running_status_replaces_legacy_success_before_any_sdk_call(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        command_output = self.command_output

        def observe(command, environment):
            running = json.loads(self.report_path.read_bytes())
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["packages"], [])
            self.assertEqual((self.root / running["previous_report"]).read_bytes(), original)
            return command_output(command, environment)

        self.command_output = observe
        self.run_verifier()

    def test_failed_initial_atomic_replace_preserves_history_and_cleans_temporary(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        replace = os.replace
        attempts = []

        def fail_first(source, destination):
            attempts.append(source)
            if len(attempts) == 1:
                raise OSError("OWNED_ATOMIC_REPLACE_FAILURE")
            return replace(source, destination)

        with patch.object(verifier.os, "replace", side_effect=fail_first):
            with self.assertRaisesRegex(OSError, "OWNED_ATOMIC_REPLACE_FAILURE"):
                self.run_verifier()
        failed = json.loads(self.report_path.read_bytes())
        self.assertEqual(failed["status"], "failed")
        self.assertEqual((self.root / failed["previous_report"]).read_bytes(), original)
        self.assertEqual(self.sdk_calls, [])
        self.assertEqual(list(self.report_path.parent.glob("*.tmp")), [])

    def test_failed_success_publication_publishes_failure_and_preserves_history(self):
        self.run_verifier()
        original = self.report_path.read_bytes()
        replace = os.replace
        attempts = []

        def fail_success(source, destination):
            attempts.append(source)
            if len(attempts) == 2:
                raise OSError("OWNED_SUCCESS_PUBLICATION_FAILURE")
            return replace(source, destination)

        with patch.object(verifier.os, "replace", side_effect=fail_success):
            with self.assertRaisesRegex(OSError, "OWNED_SUCCESS_PUBLICATION_FAILURE"):
                self.run_verifier()
        failed = json.loads(self.report_path.read_bytes())
        self.assertEqual(failed["status"], "failed")
        self.assertEqual((self.root / failed["previous_report"]).read_bytes(), original)
        self.assertEqual(list(self.report_path.parent.glob("*.tmp")), [])

    def test_persistent_atomic_write_failure_leaves_no_old_canonical_success(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        with patch.object(verifier.os, "replace", side_effect=OSError("OWNED_WRITE_UNAVAILABLE")):
            with self.assertRaisesRegex(OSError, "OWNED_WRITE_UNAVAILABLE"):
                self.run_verifier()
        self.assertFalse(self.report_path.exists())
        historical = list((self.report_path.parent / "history" / self.report_path.stem).glob("*.json"))
        self.assertEqual(len(historical), 1)
        self.assertEqual(historical[0].read_bytes(), original)
        self.assertEqual(list(self.report_path.parent.glob("*.tmp")), [])

    def test_owned_process_lock_rejects_competitor_and_releases_after_termination(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        ready = self.root / "owned-lock-ready"
        script = """
import importlib.util
from pathlib import Path
import socket
import sys
import time
def denied(*args, **kwargs):
    raise AssertionError('OWNED_LOCK_CHILD_NO_NETWORK')
socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = denied
spec = importlib.util.spec_from_file_location('owned_verifier_lock', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with module._verification_lock(Path(sys.argv[2])):
    Path(sys.argv[3]).write_text('held')
    time.sleep(10)
"""
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", script,
            str(Path(verifier.__file__).resolve()), str(self.report_path), str(ready)],
            cwd=self.root, env=dict(os.environ, LOCALAPPDATA=str(self.root / "localappdata"),
                                   APPDATA=str(self.root / "appdata")),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue("Owned process acquired its OS lock", ready.exists())
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.run_verifier()
            self.assertEqual(self.sdk_calls, [])
            self.assertEqual(self.report_path.read_bytes(), original)
            self.assertFalse((self.report_path.parent / "history").exists())
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        self.run_verifier()
        passed = json.loads(self.report_path.read_bytes())
        self.assertEqual(passed["status"], "passed")
        self.assertEqual((self.root / passed["previous_report"]).read_bytes(), original)

    def test_usage_error_preserves_canonical_and_does_not_start_an_attempt(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        for arguments in (["--version", "../invalid"], ["--version", "1.14.3", "--version-code", "0"]):
            with self.subTest(arguments=arguments), patch.object(verifier, "ROOT", self.root), \
                    patch("sys.argv", ["verify_android_apks.py", *arguments]), redirect_stdout(io.StringIO()), \
                    patch.object(verifier, "run", side_effect=AssertionError("USAGE_ERROR_REACHED_SDK")), \
                    patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    verifier.main()
                self.assertEqual(failure.exception.code, 2)
            self.assertEqual(self.report_path.read_bytes(), original)
            self.assertFalse((self.report_path.parent / "history").exists())

    def test_owned_cli_failure_exits_one_with_failed_status_and_original_history(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        script = """
import importlib.util
from pathlib import Path
import socket
import sys
def no_network(*args, **kwargs):
    raise AssertionError('OWNED_CLI_NO_NETWORK')
socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = no_network
spec = importlib.util.spec_from_file_location('owned_verifier_cli', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ROOT = Path(sys.argv[2])
def fake_sdk(*args, **kwargs):
    raise RuntimeError('OWNED_FAKE_SDK_FAILURE')
module.run = fake_sdk
sys.argv = ['verify_android_apks.py', '--version', '1.14.3', '--version-code', '20']
module.main()
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
            str(Path(verifier.__file__).resolve()), str(self.root)], cwd=self.root,
            env=dict(os.environ, LOCALAPPDATA=str(self.root / "localappdata"), APPDATA=str(self.root / "appdata")),
            capture_output=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"OWNED_FAKE_SDK_FAILURE", result.stderr)
        failed = json.loads(self.report_path.read_bytes())
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["run_id"])
        self.assertEqual((self.root / failed["previous_report"]).read_bytes(), original)

    def test_history_directory_failure_preserves_handoff_and_publishes_failed_attempt(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        historical = self.report_path.parent / "history" / self.report_path.stem
        mkdir = Path.mkdir

        def deny_history(path, *args, **kwargs):
            if path == historical:
                raise PermissionError("OWNED_HISTORY_DIRECTORY_DENIED")
            return mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", deny_history):
            with self.assertRaisesRegex(PermissionError, "OWNED_HISTORY_DIRECTORY_DENIED"):
                self.run_verifier()
        failed = json.loads(self.report_path.read_bytes())
        previous = self.root / failed["previous_report"]
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["run_id"])
        self.assertEqual(previous.parent, self.report_path.parent)
        self.assertIn(".previous-", previous.name)
        self.assertEqual(previous.read_bytes(), original)
        self.assertEqual(self.sdk_calls, [])
        self.assertEqual(list(self.report_path.parent.glob("*.tmp")), [])

    def test_history_rename_failure_preserves_handoff_and_publishes_failed_attempt(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        historical = self.report_path.parent / "history" / self.report_path.stem
        rename = Path.rename

        def deny_history(path, target):
            if Path(target).parent == historical:
                raise PermissionError("OWNED_HISTORY_RENAME_DENIED")
            return rename(path, target)

        with patch.object(Path, "rename", deny_history):
            with self.assertRaisesRegex(PermissionError, "OWNED_HISTORY_RENAME_DENIED"):
                self.run_verifier()
        failed = json.loads(self.report_path.read_bytes())
        previous = self.root / failed["previous_report"]
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["run_id"])
        self.assertEqual(previous.parent, self.report_path.parent)
        self.assertEqual(previous.read_bytes(), original)
        self.assertEqual(self.sdk_calls, [])
        self.assertEqual(list(self.report_path.parent.glob("*.tmp")), [])

    def test_initial_handoff_refusal_preserves_original_and_starts_no_sdk_work(self):
        original = b'{"packages":[], "unit_tests":[], "unit_tests_total":17}\n'
        self.report_path.write_bytes(original)
        rename = Path.rename

        def deny_initial_handoff(path, target):
            if path == self.report_path:
                raise PermissionError("OWNED_CANONICAL_SHARING_REFUSAL")
            return rename(path, target)

        with patch.object(Path, "rename", deny_initial_handoff):
            with self.assertRaisesRegex(RuntimeError, "Cannot start APK verification.*could not be preserved"):
                self.run_verifier()
        self.assertEqual(self.report_path.read_bytes(), original)
        self.assertEqual(self.sdk_calls, [])
        self.assertFalse((self.report_path.parent / "history").exists())
        self.run_verifier()
        self.assertEqual(json.loads(self.report_path.read_bytes())["status"], "passed")

    def test_atomic_temporary_collision_preserves_preexisting_sentinel(self):
        original = b'{"status":"passed","owned_previous_report":true}\n'
        self.report_path.write_bytes(original)
        collision = SimpleNamespace(hex="ownedcollision")
        temporary = self.report_path.with_name("." + self.report_path.name + ".ownedcollision.tmp")
        sentinel = b"PREEXISTING_TEMPORARY_NOT_CREATED_BY_THIS_ATTEMPT"
        temporary.write_bytes(sentinel)
        with patch.object(verifier, "uuid4", return_value=collision):
            with self.assertRaises(FileExistsError):
                verifier._atomic_json(self.report_path, {"status": "running"})
        self.assertEqual(temporary.read_bytes(), sentinel)
        self.assertEqual(self.report_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

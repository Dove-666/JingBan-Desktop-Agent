"""Offline Docker simulator only. This suite must never contact a daemon."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot import container_execution as container
from agent_backend.nanobot.safe_files import WorkspaceFiles


IMAGE = "aura-controlled/python@sha256:" + "1" * 64
PRIVATE = "SYNTHETIC_AUTHORIZATION_BEARER_DO_NOT_EXPORT"


class FakeDocker:
    """Models engine state separately from the lifetime of its attached client."""
    def __init__(self):
        self.calls = []
        self.records = {}
        self.outputs = {"result.csv": b"name,total\nfixture,42\n"}
        self.start_state = "OK"
        self.create_state = "OK"
        self.create_visible = True
        self.remove_fails = False
        self.list_fails = False
        self.inspect_fails = False
        self.reap_result = True
        self.report_wrong_cid = False
        self.tamper = None
        self.start_callback = None
        self.image_environment = ["PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8"]
        self.stdout = b"isolated fixture output"
        self.stderr = PRIVATE.encode()
        self.exit_code = 0

    @staticmethod
    def result(value):
        return container.DockerCommandResult("OK", 0, json.dumps(value).encode())

    def call(self, argv, **options):
        self.calls.append((tuple(argv), options))
        assert argv[0] == str(getattr(self, "docker_cli_path", container.DOCKER_CLI))
        assert argv[1] == "--config" and argv[3:5] == ["--host", container.DOCKER_ENDPOINT]
        args = argv[5:]
        if args[:2] == ["image", "inspect"]:
            return self.result({"Os": "linux", "RepoDigests": [IMAGE], "Config": {"Volumes": None, "Env": self.image_environment}})
        if args[:2] == ["container", "create"]:
            get = lambda flag: args[args.index(flag) + 1]
            many = lambda flag: [args[index + 1] for index, value in enumerate(args) if value == flag]
            name = get("--name")
            mounts = []
            for value in many("--mount"):
                fields = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
                mounts.append({"Type": "bind", "Source": fields["source"], "Destination": fields["target"],
                    "RW": "readonly" not in value.split(","), "Propagation": fields["bind-propagation"]})
            limits = []
            for value in many("--ulimit"):
                key, values = value.split("="); soft, hard = map(int, values.split(":"))
                limits.append({"Name": key, "Soft": soft, "Hard": hard})
            temporary = dict(value.split(":", 1) for value in many("--tmpfs"))
            cid = hashlib.sha256(name.encode()).hexdigest()
            record = {"Id": cid, "Name": "/" + name, "Config": {
                "Labels": dict(value.split("=", 1) for value in many("--label")), "User": get("--user"),
                "Entrypoint": [get("--entrypoint")], "Image": IMAGE, "Cmd": ["-I", "-B", "/aura/input/.aura_container_entry.py"],
                "WorkingDir": get("--workdir"), "OpenStdin": "--interactive" in args, "Env": self.image_environment},
                "HostConfig": {"NetworkMode": get("--network"), "ReadonlyRootfs": "--read-only" in args,
                    "CapDrop": many("--cap-drop"), "CapAdd": None, "Privileged": False, "PidMode": "", "Devices": [],
                    "Runtime": "runc", "Binds": None, "Links": None, "DeviceRequests": None, "DeviceCgroupRules": None,
                    "VolumesFrom": [], "PortBindings": {}, "PublishAllPorts": False, "IpcMode": get("--ipc"),
                    "CgroupnsMode": get("--cgroupns"), "RestartPolicy": {"Name": get("--restart")},
                    "LogConfig": {"Type": get("--log-driver")}, "PidsLimit": int(get("--pids-limit")),
                    "Memory": int(get("--memory").removesuffix("m")) * 1024 * 1024,
                    "MemorySwap": int(get("--memory-swap").removesuffix("m")) * 1024 * 1024,
                    "NanoCpus": int(float(get("--cpus")) * 1e9), "SecurityOpt": many("--security-opt"),
                    "Tmpfs": temporary, "Ulimits": limits},
                "Mounts": mounts, "State": {"Running": False, "ExitCode": 0}}
            self.records[name] = {"metadata": record, "exists": self.create_visible}
            if self.create_state != "OK": return container.DockerCommandResult(self.create_state)
            return container.DockerCommandResult("OK", 0, (("b" * 64) if self.report_wrong_cid else cid).encode())
        name = args[-1]
        if args[:2] == ["container", "inspect"]:
            record = next((item for item in self.records.values() if item["metadata"]["Id"] == name or item["metadata"]["Name"] == "/" + name), None)
            if self.inspect_fails or not record or not record["exists"]: return container.DockerCommandResult("OK", 1)
            value = deepcopy(record["metadata"])
            if self.tamper: self.tamper(value)
            return self.result(value)
        if args[:2] == ["container", "start"]:
            record = self.records[name]
            record["metadata"]["State"]["Running"] = True
            # The container's virtual tmpfs never maps onto a host RW path.
            record["simulated_tmpfs"] = dict(self.outputs)
            if self.start_callback: self.start_callback(record["metadata"], options)
            if self.start_state != "OK": return container.DockerCommandResult(self.start_state,
                client_reaped=self.start_state != "CLIENT_REAP_UNCONFIRMED")
            if options.get("watch") and not options["watch"](): return container.DockerCommandResult("OUTPUT_LIMIT")
            record["metadata"]["State"] = {"Running": False, "ExitCode": self.exit_code}
            return container.DockerCommandResult("OK", self.exit_code, self.stdout, self.stderr)
        if args[:2] == ["container", "rm"]:
            if self.remove_fails: return container.DockerCommandResult("OK", 1)
            name = next(key for key, record in self.records.items() if record["metadata"]["Id"] == name and record["exists"])
            self.records[name]["exists"] = False
            self.records[name]["metadata"]["State"]["Running"] = False
            return container.DockerCommandResult("OK", 0, name.encode())
        if args[:2] == ["container", "ls"]:
            if self.list_fails: return container.DockerCommandResult("CLIENT_FAILED")
            selected = args[args.index("--filter") + 1]
            if selected.startswith("id="):
                record = next((item for item in self.records.values() if item["metadata"]["Id"] == selected[3:]), None)
            else:
                needle = selected.removeprefix("name=^/").removesuffix("$")
                record = next((item for item in self.records.values() if item["metadata"]["Name"] == "/" + needle), None)
            return container.DockerCommandResult("OK", 0, record["metadata"]["Id"].encode() if record and record["exists"] else b"")
        raise AssertionError("Unrecognized fake Docker action")

    def reap_pending(self, **_):
        return self.reap_result


class ContainerExecutionTests(unittest.TestCase):
    def test_package_import_uses_same_workspace_type(self):
        self.assertIs(container.WorkspaceFiles, WorkspaceFiles)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe").is_file(),
                         "isolated backend Python missing")
    def test_isolated_worker_style_import_uses_same_workspace_type(self):
        backend = Path(__file__).resolve().parents[1]
        script = """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import safe_files
import container_execution
assert container_execution.WorkspaceFiles is safe_files.WorkspaceFiles
transport = container_execution.DockerCLITransport()
assert transport.capability()['available'] is False
print(json.dumps({'standalone_import': True, 'same_workspace_type': True, 'live_enabled': False}))
"""
        result = subprocess.run([str(backend / ".venv" / "Scripts" / "python.exe"), "-I", "-c", script, str(backend)],
                                capture_output=True, timeout=15, check=False)
        self.assertEqual(0, result.returncode, "Isolated standalone import failed")
        self.assertEqual({"standalone_import": True, "same_workspace_type": True, "live_enabled": False},
                         json.loads(result.stdout))
        self.assertEqual(b"", result.stderr)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-container-test-")
        self.workspace = Path(self.directory.name) / "workspace"; self.workspace.mkdir()
        self.files = WorkspaceFiles(self.workspace)
        self.current = True
        self.scope = self.new_scope()
        self.fake = FakeDocker()
        self.runner = container.ContainerPythonRunner(container.ContainerPolicy(IMAGE), transport=self.fake, known_secrets=(PRIVATE,))

    def tearDown(self):
        self.current = True
        self.fake.remove_fails = self.fake.list_fails = self.fake.inspect_fails = False
        self.fake.tamper = None
        # The simulator owns no real containers. Release only this test's
        # physically checked scratch after simulating confirmed absence.
        for job in list(self.runner._jobs.values()):
            if job.name in self.fake.records: self.fake.records[job.name]["exists"] = False
            (job.root / "owner.json").write_text(json.dumps({"name": job.name, "owner": job.owner, "scope": job.scope.identity}), encoding="utf-8")
            if self.runner.retry_cleanup(job.name, scope=job.scope): self.runner.dispose(job.name, scope=job.scope)
        self.directory.cleanup()

    def new_scope(self, **changes):
        data = {"conversation_id": str(uuid4()), "generation": str(uuid4()), "request_id": str(uuid4()),
                "files": self.files, "grant_revision": "f" * 64, "is_current": lambda: self.current}
        data.update(changes)
        return container.HostExecutionScope(**data)

    def run_fixture(self, **options):
        return self.runner.execute("print(input['value'])", {"value": 42}, scope=self.scope, **options)

    def test_native_default_and_import_are_inert_and_never_claim_runtime_available(self):
        with patch("subprocess.Popen", side_effect=AssertionError("No actual Docker")), \
             patch("tempfile.mkdtemp", side_effect=AssertionError("No staging for disabled backend")):
            native = container.ContainerPythonRunner(container.ContainerPolicy(IMAGE))
            result = native.execute("print(1)", {}, scope=None)
            self.assertEqual(result.code, "CONTAINER_RUNTIME_UNVERIFIED")
            self.assertFalse(native.capability()["available"])
        self.assertFalse(self.runner.capability()["available"])

    def test_import_does_not_start_io_threads_or_sdk_or_discover_docker(self):
        with patch("subprocess.Popen", side_effect=AssertionError("No process on import")), \
             patch("threading.Thread.start", side_effect=AssertionError("No thread on import")), \
             patch("pathlib.Path.open", side_effect=AssertionError("No user files on import")), \
             patch("tempfile.mkdtemp", side_effect=AssertionError("No scratch on import")):
            importlib.reload(container)

    def test_environment_does_not_even_read_ambient_docker_or_api_variables(self):
        class RestrictedEnvironment:
            def get(self, key):
                if key not in ("SystemRoot", "WINDIR", "SystemDrive"):
                    raise AssertionError("Unexpected environment lookup")
                return "C:\\Windows" if key != "SystemDrive" else "C:"
        with patch.object(container.os, "environ", RestrictedEnvironment()):
            values = self.runner._environment(SimpleNamespace(root=Path(self.directory.name)))
        self.assertFalse(any(key.startswith("DOCKER") for key in values))

    def test_docker_cli_path_is_only_explicit_local_host_configuration(self):
        configured = r"C:\Users\HostFixture\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"
        with patch("subprocess.Popen", side_effect=AssertionError("Default gate must stay closed")):
            native = container.DockerCLITransport(docker_cli_path=configured)
            self.assertEqual(str(native.docker_cli_path), str(Path(configured)))
            self.assertFalse(native.capability()["available"])
        self.fake.docker_cli_path = Path(configured)
        result = self.run_fixture()
        self.assertTrue(result.container_removed)
        self.assertTrue(all(argv[0] == str(Path(configured)) for argv, _ in self.fake.calls))
        for invalid in ("docker.exe", r"\\remote\share\docker.exe", r"C:\tools\pwsh.exe", r"C:\tools\..\docker.exe"):
            with self.assertRaisesRegex(container.ContainerExecutionError, "CONTAINER_INVALID_DOCKER_CLI"):
                container.DockerCLITransport(docker_cli_path=invalid)

    def test_exact_fixed_endpoint_flags_no_inherited_config_or_secret_environment(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://foreign.invalid:2375", "DOCKER_CONTEXT": "foreign",
            "DOCKER_CONFIG": r"C:\Users\PrivatePerson\.docker", "OPENAI_API_KEY": PRIVATE, "HTTP_PROXY": PRIVATE}):
            result = self.run_fixture()
        self.assertEqual(result.state, "exited"); self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.container_removed); self.assertFalse(result.task_verified)
        self.assertNotIn(PRIVATE, result.stderr)
        create = next(argv for argv, _ in self.fake.calls if argv[5:7] == ("container", "create"))
        for required in ("--pull=never", "--read-only", "ALL", "no-new-privileges=true", "10001:10001", "--no-healthcheck", "--log-driver", "--memory-swap"):
            self.assertIn(required, create)
        self.assertEqual(create[-4:], (IMAGE, "-I", "-B", "/aura/input/.aura_container_entry.py"))
        for argv, options in self.fake.calls:
            self.assertNotIn("--context", argv)
            self.assertNotIn("pull", argv)
            self.assertFalse(any("DOCKER_" in key or "KEY" in key or "PROXY" in key for key in options["environment"]))
            self.assertNotIn(PRIVATE, str(options["environment"]))
            self.assertEqual(argv[4], container.DOCKER_ENDPOINT)
        job = self.runner._jobs[result.job_id]
        self.assertFalse((job.root / "quarantine").exists())
        metadata = self.fake.records[result.job_id]["metadata"]
        self.assertEqual(len(metadata["Mounts"]), 1)
        self.assertFalse(metadata["Mounts"][0]["RW"])
        self.assertIn("size=16777216", metadata["HostConfig"]["Tmpfs"]["/aura/output"])
        self.assertIn("noexec", metadata["HostConfig"]["Tmpfs"]["/aura/output"])
        self.assertEqual(result.artifacts, ())

    def test_explicit_inputs_are_hashed_copies_and_workspace_is_never_mounted(self):
        path = self.workspace / "source.csv"; raw = b"value\n42\n"; path.write_bytes(raw)
        item = container.AuthorizedInput("source.csv", hashlib.sha256(raw).hexdigest(), "input.csv")
        def inspect_staging(metadata, options):
            mounts = metadata["Mounts"]
            source = next(row for row in mounts if row["Destination"] == "/aura/input")
            self.assertFalse(source["RW"])
            self.assertEqual((Path(source["Source"]) / "input.csv").read_bytes(), raw)
            path.write_bytes(b"host changed after copy")
            self.assertEqual((Path(source["Source"]) / "input.csv").read_bytes(), raw)
            self.assertFalse(any(Path(row["Source"]).is_relative_to(self.workspace) for row in mounts))
            payload = json.loads(options["stdin"])
            self.assertEqual(set(payload), {"version", "code", "input"})
        self.fake.start_callback = inspect_staging
        result = self.run_fixture(authorized_inputs=[item])
        self.assertEqual(result.state, "exited")

    def test_image_must_be_host_pinned_and_image_secrets_or_volumes_are_rejected(self):
        for image in ("python:latest", "python@sha256:123", "--privileged image", "python@sha256:" + "A" * 64):
            with self.assertRaisesRegex(container.ContainerExecutionError, "CONTAINER_PINNED_IMAGE_REQUIRED"):
                container.ContainerPolicy(image).validate()
        self.fake.image_environment += ["OPENAI_API_KEY=" + PRIVATE]
        result = self.run_fixture()
        self.assertEqual(result.code, "CONTAINER_IMAGE_ENV_REJECTED")
        self.assertFalse(any(argv[5:7] == ("container", "create") for argv, _ in self.fake.calls))

    def test_control_drift_is_rejected_before_start_and_owned_container_removed(self):
        self.fake.tamper = lambda value: value["HostConfig"].update({"Privileged": True})
        result = self.run_fixture()
        self.assertEqual(result.code, "CONTAINER_POLICY_NOT_ENFORCED")
        self.assertTrue(result.container_removed)
        self.assertFalse(any(argv[5:7] == ("container", "start") for argv, _ in self.fake.calls))

    def test_tmpfs_conflicts_foreign_pid_namespace_and_extra_mounts_fail_closed(self):
        mutations = [
            lambda value: value["HostConfig"]["Tmpfs"].__setitem__("/aura/output", value["HostConfig"]["Tmpfs"]["/aura/output"] + ",exec"),
            lambda value: value["HostConfig"]["Tmpfs"].__setitem__("/aura/output", value["HostConfig"]["Tmpfs"]["/aura/output"] + ",size=999999999999"),
            lambda value: value["HostConfig"]["Tmpfs"].__setitem__("/aura/output", value["HostConfig"]["Tmpfs"]["/aura/output"] + ",uid=0"),
            lambda value: value["HostConfig"].__setitem__("PidMode", "container:foreign-id"),
            lambda value: value["HostConfig"].__setitem__("Memory", 0),
            lambda value: value["HostConfig"].__setitem__("CapAdd", ["SYS_ADMIN"]),
            lambda value: value["Mounts"][0].__setitem__("RW", True),
            lambda value: value["Mounts"][0].__setitem__("Propagation", "rshared"),
            lambda value: value["HostConfig"]["Ulimits"].append(dict(value["HostConfig"]["Ulimits"][0])),
            lambda value: value["HostConfig"]["Ulimits"].append(None),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.fake.tamper = mutate
                previous = len(self.fake.calls)
                result = self.run_fixture()
                self.assertEqual(result.code, "CONTAINER_POLICY_NOT_ENFORCED")
                self.assertTrue(result.container_removed)
                self.assertFalse(any(argv[5:7] == ("container", "start") for argv, _ in self.fake.calls[previous:]))

    def test_wrong_reported_container_id_never_starts_and_cleanup_uses_verified_owned_name(self):
        self.fake.report_wrong_cid = True
        result = self.run_fixture()
        self.assertEqual(result.code, "CONTAINER_IDENTITY_MISMATCH")
        self.assertTrue(result.container_removed)
        self.assertFalse(any(argv[5:7] == ("container", "start") for argv, _ in self.fake.calls))
        inspected_cid = self.fake.records[result.job_id]["metadata"]["Id"]
        self.assertTrue(any(argv[5:7] == ("container", "rm") and argv[-1] == inspected_cid for argv, _ in self.fake.calls))

    def test_foreign_container_with_same_name_is_never_removed(self):
        self.fake.tamper = lambda value: value["Config"]["Labels"].update({container._OWNER_LABEL: "foreign-owner"})
        result = self.run_fixture()
        self.assertEqual(result.state, "cleanup_unconfirmed")
        self.assertFalse(result.container_removed)
        self.assertFalse(any(argv[5:7] == ("container", "rm") for argv, _ in self.fake.calls))

    def test_timeout_and_cancel_remove_running_container_not_just_attached_client(self):
        for state in ("TIMEOUT", "CANCELLED"):
            with self.subTest(state=state):
                self.fake.start_state = state
                result = self.run_fixture()
                self.assertEqual(result.state, "timeout" if state == "TIMEOUT" else "cancelled")
                self.assertTrue(result.container_removed); self.assertFalse(self.fake.records[result.job_id]["exists"])
                self.assertEqual(result.artifacts, ())

    def test_cancelled_create_with_late_visibility_remains_unconfirmed_until_owned_cleanup(self):
        self.fake.create_state = "CANCELLED"; self.fake.create_visible = False
        result = self.run_fixture()
        self.assertEqual(result.state, "cleanup_unconfirmed")
        self.assertTrue((self.runner._jobs[result.job_id].root / "owner.json").is_file())
        with self.assertRaisesRegex(container.ContainerExecutionError, "CONTAINER_CLEANUP_UNCONFIRMED"):
            self.runner.dispose(result.job_id, scope=self.scope)
        self.fake.records[result.job_id]["exists"] = True
        self.current = False  # Stop authority remains after data grants revoke.
        self.assertTrue(self.runner.retry_cleanup(result.job_id, scope=self.scope))

    def test_remove_or_confirmation_failure_never_publishes_or_deletes_scratch(self):
        self.fake.remove_fails = True
        result = self.run_fixture()
        self.assertEqual(result.state, "cleanup_unconfirmed"); self.assertEqual(result.artifacts, ())
        self.assertTrue(self.runner._jobs[result.job_id].root.exists())

    def test_removed_container_with_unreaped_client_still_has_unconfirmed_cleanup(self):
        self.fake.start_state = "CLIENT_REAP_UNCONFIRMED"; self.fake.reap_result = False
        result = self.run_fixture()
        self.assertEqual(result.state, "cleanup_unconfirmed")
        self.assertFalse(self.fake.records[result.job_id]["exists"])
        self.assertTrue(self.runner._jobs[result.job_id].root.exists())
        self.fake.reap_result = True
        self.assertTrue(self.runner.retry_cleanup(result.job_id, scope=self.scope))

    def test_admin_rename_cannot_be_mistaken_for_owned_container_removal(self):
        # Terminal inspect still observes the original name. Simulate a rename
        # just before cleanup begins, after the instance ID was verified.
        original_cleanup = self.runner._cleanup
        def renamed_cleanup(job):
            self.fake.records[job.name]["metadata"]["Name"] = "/renamed-owned-fixture"
            return original_cleanup(job)
        with patch.object(self.runner, "_cleanup", renamed_cleanup):
            result = self.run_fixture()
        self.assertTrue(result.container_removed)
        record = self.fake.records[result.job_id]
        self.assertFalse(record["exists"])
        owned_id = record["metadata"]["Id"]
        self.assertTrue(any(argv[5:7] == ("container", "inspect") and argv[-1] == owned_id for argv, _ in self.fake.calls))
        self.assertTrue(any("id=" + owned_id in argv for argv, _ in self.fake.calls))

    def test_invalid_hash_ungranted_file_and_reparse_input_never_create_container(self):
        source = self.workspace / "source.csv"; source.write_bytes(b"fixture")
        invalid = container.AuthorizedInput("source.csv", "0" * 64, "input.csv")
        result = self.run_fixture(authorized_inputs=[invalid])
        self.assertEqual(result.code, "CONTAINER_INPUT_CHANGED"); self.assertEqual(self.fake.calls, [])
        outside = Path(self.directory.name) / "outside.csv"; outside.write_bytes(b"private fixture")
        result = self.run_fixture(authorized_inputs=[container.AuthorizedInput(str(outside), "0" * 64, "outside.csv")])
        self.assertEqual(result.code, "CONTAINER_INPUT_NOT_AUTHORIZED"); self.assertEqual(self.fake.calls, [])
        actual_lstat = Path.lstat
        def lstat(path):
            original = actual_lstat(path)
            if path == source: return SimpleNamespace(st_mode=original.st_mode, st_file_attributes=0x400)
            return original
        with patch.object(Path, "lstat", lstat):
            result = self.run_fixture(authorized_inputs=[container.AuthorizedInput("source.csv", hashlib.sha256(b"fixture").hexdigest(), "input.csv")])
        self.assertEqual(result.code, "CONTAINER_PATH_ALIAS_REJECTED"); self.assertEqual(self.fake.calls, [])

    def test_hardlinked_inputs_and_case_alias_collisions_are_rejected(self):
        source = self.workspace / "source.csv"; source.write_bytes(b"fixture")
        alias = self.workspace / "linked.csv"
        os.link(source, alias)
        expected = hashlib.sha256(b"fixture").hexdigest()
        result = self.run_fixture(authorized_inputs=[container.AuthorizedInput("source.csv", expected, "input.csv")])
        self.assertEqual(result.code, "CONTAINER_INPUT_LINK_REJECTED")
        alias.unlink()
        result = self.run_fixture(authorized_inputs=[container.AuthorizedInput("source.csv", expected, "input.csv"),
            container.AuthorizedInput("source.csv", expected, "INPUT.CSV")])
        self.assertEqual(result.code, "CONTAINER_INVALID_INPUTS")
        self.assertEqual(self.fake.calls, [])

    def test_stdin_budget_rejects_huge_cyclic_non_json_or_nonfinite_data_before_staging(self):
        cyclic = []; cyclic.append(cyclic)
        for value in ("x" * (2 * 1024 * 1024 + 1), [None] * 100001, cyclic, float("nan"), {1: "not-a-json-key"}):
            with self.subTest(kind=type(value).__name__), patch("tempfile.mkdtemp", side_effect=AssertionError("No staging invalid input")):
                with self.assertRaises(container.ContainerExecutionError):
                    self.runner.execute("print(input)", value, scope=self.scope)
        self.assertEqual(self.fake.calls, [])

    def test_owner_marker_replacement_blocks_public_cleanup(self):
        result = self.run_fixture(); job = self.runner._jobs[result.job_id]
        (job.root / "owner.json").write_text('{"owner":"foreign"}', encoding="utf-8")
        with self.assertRaisesRegex(container.ContainerExecutionError, "CONTAINER_SCRATCH_CHANGED"):
            self.runner.dispose(result.job_id, scope=self.scope)
        self.assertTrue(job.root.exists())

    def test_scope_mismatch_generation_and_request_changes_cannot_publish(self):
        result = self.run_fixture()
        for other in (self.new_scope(), self.new_scope(conversation_id=self.scope.conversation_id),
                      self.new_scope(conversation_id=self.scope.conversation_id, generation=self.scope.generation)):
            with self.assertRaisesRegex(container.ContainerExecutionError, "CONTAINER_SCOPE_MISMATCH"):
                self.runner.retry_cleanup(result.job_id, scope=other)
        self.assertFalse((self.workspace / "out.csv").exists())
        with self.assertRaises(FrozenInstanceError): self.scope.generation = str(uuid4())

    def test_revoked_scope_stops_running_job_and_never_returns_its_output(self):
        self.fake.start_callback = lambda *_: setattr(self, "current", False)
        result = self.run_fixture()
        self.assertEqual(result.code, "CONTAINER_SCOPE_REVOKED"); self.assertTrue(result.container_removed)
        self.assertEqual(result.stdout, ""); self.assertEqual(result.artifacts, ())

    def test_file_publication_is_unavailable_and_cannot_write_or_execute_on_host(self):
        result = self.run_fixture()
        for target in ("out.csv", "script.py", "../outside.csv"):
            with self.assertRaisesRegex(container.ContainerExecutionError, "CONTAINER_FILE_EXPORT_UNAVAILABLE"):
                self.runner.publish(result.job_id, "untrusted-reference", target, scope=self.scope)
        self.assertFalse((self.workspace / "out.csv").exists())
        self.assertFalse((self.workspace / "script.py").exists())

    def test_virtual_tmpfs_files_are_never_exported_or_executed(self):
        self.fake.outputs = {"do-not-run.py": b"raise RuntimeError('must never execute')"}
        result = self.run_fixture()
        self.assertEqual(result.artifacts, ())
        self.assertFalse((self.workspace / "do-not-run.py").exists())
        self.assertFalse(any(argv[5:7] == ("container", "cp") for argv, _ in self.fake.calls))

    def test_stdout_stderr_have_separate_bounds(self):
        self.fake.stdout = b"x" * (self.runner.policy.stdout_limit + 1)
        result = self.run_fixture(); self.assertEqual(result.code, "CONTAINER_OUTPUT_LIMIT")
        self.fake.stdout = b"safe"; self.fake.stderr = b"x" * (self.runner.policy.stderr_limit + 1)
        result = self.run_fixture(); self.assertEqual(result.code, "CONTAINER_OUTPUT_LIMIT")


class NativeDockerTransportOfflineTests(unittest.TestCase):
    def test_unreaped_client_is_not_reported_stopped_and_can_be_retried(self):
        class Process:
            def __init__(self):
                self.stdout = io.BytesIO(); self.stderr = io.BytesIO(); self.alive = True
            def poll(self): return None if self.alive else 0
            def kill(self): pass  # Simulates an OS/client failure to terminate.
            def wait(self, timeout):
                if self.alive: raise subprocess.TimeoutExpired("controlled-fake-client", timeout)
                return 0
        process = Process()
        transport = container.DockerCLITransport(live_enabled=True)
        argv = [str(container.DOCKER_CLI), "--config", "owned-client", "--host", container.DOCKER_ENDPOINT,
                "container", "inspect", "aura-py-" + "a" * 32]
        with patch.object(container.sys, "platform", "win32"), patch.object(container.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             patch.object(container.subprocess, "Popen", return_value=process), patch.object(container, "_read_available", return_value=b""):
            result = transport.call(argv, stdin=None, environment={}, cwd="owned-client", timeout=.001,
                cancel_event=threading.Event(), stdout_limit=1024, stderr_limit=1024)
        self.assertEqual(result.state, "CLIENT_REAP_UNCONFIRMED"); self.assertFalse(result.client_reaped)
        self.assertFalse(transport.reap_pending(timeout=.01))
        process.alive = False
        self.assertTrue(transport.reap_pending(timeout=.01))

    def test_endpoint_context_or_secret_env_override_is_rejected_before_process_start(self):
        transport = container.DockerCLITransport(live_enabled=True)
        base = [str(container.DOCKER_CLI), "--config", "owned-client", "--host", container.DOCKER_ENDPOINT, "container", "inspect", "owned-name"]
        with patch.object(container.sys, "platform", "win32"), patch.object(container.subprocess, "Popen", side_effect=AssertionError("Must reject before launch")):
            for argv, environment in ((base + ["--host", "tcp://foreign.invalid"], {}),
                (base + ["--context=foreign"], {}), (base, {"DOCKER_HOST": "tcp://foreign.invalid"}), (base, {"API_KEY": PRIVATE})):
                result = transport.call(argv, stdin=None, environment=environment, cwd="owned-client", timeout=1,
                    cancel_event=threading.Event(), stdout_limit=1024, stderr_limit=1024)
                self.assertEqual(result.state, "CLIENT_FAILED")

    def test_pipe_close_errors_cannot_lose_unreaped_client_tracking(self):
        class FailingPipe:
            def close(self): raise OSError("controlled-close-failure")
        class Process:
            stdout = FailingPipe(); stderr = FailingPipe()
            alive = True
            def poll(self): return None if self.alive else 0
            def kill(self): pass
            def wait(self, timeout):
                if self.alive: raise subprocess.TimeoutExpired("controlled-client", timeout)
                return 0
        process = Process(); transport = container.DockerCLITransport(live_enabled=True)
        argv = [str(container.DOCKER_CLI), "--config", "owned-client", "--host", container.DOCKER_ENDPOINT,
                "container", "inspect", "owned-name"]
        with patch.object(container.sys, "platform", "win32"), patch.object(container.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             patch.object(container.subprocess, "Popen", return_value=process), patch.object(container, "_read_available", return_value=b""):
            result = transport.call(argv, stdin=None, environment={}, cwd="owned-client", timeout=.001,
                cancel_event=threading.Event(), stdout_limit=1024, stderr_limit=1024)
        self.assertFalse(result.client_reaped); self.assertEqual(result.state, "CLIENT_REAP_UNCONFIRMED")
        self.assertFalse(transport.reap_pending(timeout=.01))
        process.alive = False
        self.assertTrue(transport.reap_pending(timeout=.01))
if __name__ == "__main__":
    unittest.main()

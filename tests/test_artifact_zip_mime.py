"""Verified ZIP delivery uses a portable MIME despite Windows associations."""
import base64
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_backend/nanobot"))
from artifact_verifier import ArtifactVerifier
from safe_files import WorkspaceFiles
from desktop_pet.agents.artifacts import ArtifactStore


class ArtifactZipMimeTests(unittest.TestCase):
    def test_windows_associations_cannot_change_verified_zip_delivery_metadata(self):
        with tempfile.TemporaryDirectory(prefix="aura-zip-mime-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"; workspace.mkdir()
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("owned.txt", b"FIXED_OWNED_CONTENT")
            files = WorkspaceFiles(workspace)
            store = ArtifactStore(root / "delivery")
            for name in ("report.zip", "REPORT.ZIP"):
                (workspace / name).write_bytes(payload.getvalue())
                report = {"path": name, **ArtifactVerifier(workspace).verify(name)}
                self.assertTrue(report["passed"])
                for association in ("application/x-zip-compressed", "unexpected/custom", None):
                    with self.subTest(name=name, association=association):
                        cid = str(uuid4())
                        with patch("desktop_pet.agents.artifacts.mimetypes.guess_type", return_value=(association, None)):
                            metadata = store.register(cid, report, files)
                        self.assertEqual("application/zip", metadata["mime"])
                        page = store.get(cid, metadata["reference"])
                        self.assertEqual(metadata["mime"], page["mime"])
                        self.assertEqual(payload.getvalue(), base64.b64decode(page["data_base64"], validate=True))


if __name__ == "__main__": unittest.main()

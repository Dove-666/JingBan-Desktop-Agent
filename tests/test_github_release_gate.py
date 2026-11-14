"""Release publishing must use the exact accepted bytes and commit."""
import unittest
from types import SimpleNamespace
from tools.qa_release.publish_github import verify_release_target, verify_remote_assets


class ReleaseIdentityTests(unittest.TestCase):
    def test_same_size_replacement_does_not_pass_digest_check(self):
        expected = [{"name": "app.apk", "bytes": 20, "sha256": "a" * 64}]
        remote = [{"name": "app.apk", "size": 20, "state": "uploaded", "digest": "sha256:" + "b" * 64}]
        with self.assertRaisesRegex(RuntimeError, "bytes differ"):
            verify_remote_assets(remote, expected)
        remote[0]["digest"] = "sha256:" + "a" * 64
        verify_remote_assets(remote, expected)

    def test_missing_digest_or_extra_asset_cannot_publish(self):
        expected = [{"name": "app.apk", "bytes": 20, "sha256": "a" * 64}]
        remote = [{"name": "app.apk", "size": 20, "state": "uploaded"}]
        with self.assertRaises(RuntimeError):
            verify_remote_assets(remote, expected)
        with self.assertRaises(RuntimeError):
            verify_remote_assets(remote * 2, expected)

    def test_preexisting_tag_must_still_match_commit(self):
        record = {"tag": "v1.0.2", "commit": "a" * 40}
        release = {"tag_name": record["tag"], "target_commitish": record["commit"], "draft": True}
        response = SimpleNamespace(status_code=200, json=lambda: {"object": {"type": "commit", "sha": "b" * 40}})
        client = SimpleNamespace(get=lambda *a, **kw: response)
        with self.assertRaisesRegex(RuntimeError, "tag target changed"):
            verify_release_target(client, release, record)
        response.json = lambda: {"object": {"type": "commit", "sha": record["commit"]}}
        verify_release_target(client, release, record)

    def test_only_unpublished_draft_may_have_no_tag(self):
        record = {"tag": "v1.0.2", "commit": "a" * 40}
        release = {"tag_name": record["tag"], "target_commitish": record["commit"], "draft": True}
        client = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(status_code=404))
        verify_release_target(client, release, record)
        release["draft"] = False
        with self.assertRaises(RuntimeError):
            verify_release_target(client, release, record)


if __name__ == "__main__":
    unittest.main()

"""Offline resource correction for the preserved, unclean before fixture.

Two expected failures remain product recovery gaps, not successful fixes.
The three safety negatives exercise their assertions without resource DNS.
"""
import hashlib
from itertools import islice
from pathlib import Path
import stat
import unittest

from tests import gui_no_effect_recovery_fixtures as before


ASSET_NAME = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
ASSET_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
used_assets = []


def _verified_public_asset():
    """Bounded repo-local candidates only; never fetch or inspect user caches."""
    backend = before.fixture.BACKEND
    roots = [backend / "assets" / "tiktoken", backend / ".cache" / "tiktoken"]
    runtime = backend / "runtime"
    try:
        info = runtime.lstat()
        if stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400:
            roots += [entry / "cache" / "tiktoken" for entry in islice(runtime.iterdir(), 512)]
    except OSError:
        pass
    for directory in roots:
        candidate = directory / ASSET_NAME
        try:
            relative = candidate.relative_to(backend)
            current = backend
            for part in relative.parts:
                current /= part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    break
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_size != 1681126:
                    continue
                with candidate.open("rb") as stream:
                    raw = stream.read(1681127)
                if hashlib.sha256(raw).hexdigest() == ASSET_SHA256:
                    used_assets.append(str(candidate.relative_to(before.fixture.REPO)))
                    return raw
        except OSError:
            continue
    raise unittest.SkipTest("Verified local public tokenizer asset unavailable; no download permitted")


def seed_owned_tokenizer_cache(workspace: Path) -> Path:
    """Reuse only an already-local, hash-verified public tokenizer asset."""
    raw = _verified_public_asset()
    cache = workspace / "owned-offline-tokenizer-cache"
    cache.mkdir()
    (cache / ASSET_NAME).write_bytes(raw)
    return cache


def install_owned_cache(code: str, cache: Path) -> str:
    anchor = "class OwnedGui(worker.GuiBridge):"
    if code.count(anchor) != 1:
        raise AssertionError("FIXTURE_CACHE_INSERTION_ANCHOR_CHANGED")
    setup = ("os.environ['TIKTOKEN_CACHE_DIR']=" + repr(str(cache)) + "\n"
             "import tiktoken\n"
             "counts['offline_tokenizer_ready']=tiktoken.get_encoding('cl100k_base').name=='cl100k_base'\n")
    return code.replace(anchor, setup + anchor)


class GuiNoEffectRecoveryOfflineTests(before.GuiNoEffectRecoveryBeforeTests):
    observations = []

    def wrapper(self, mode):
        cache = seed_owned_tokenizer_cache(self.workspace)
        wrapper, output = super().wrapper(mode)
        wrapper.write_text(install_owned_cache(wrapper.read_text(encoding="utf-8"), cache), encoding="utf-8")
        return wrapper, output

    @unittest.expectedFailure
    def test_pure_file_goal_after_zero_input_screenshot_failure_can_finish(self):
        super().test_pure_file_goal_after_zero_input_screenshot_failure_can_finish()

    @unittest.expectedFailure
    def test_pure_file_goal_after_readonly_observation_failure_can_finish(self):
        super().test_pure_file_goal_after_readonly_observation_failure_can_finish()


if __name__ == "__main__":
    unittest.main()

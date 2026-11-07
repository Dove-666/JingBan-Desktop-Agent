"""Frozen read-only resources and an explicit launch profile for child workers."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys


PROFILE_ENVIRONMENT = "JINGBAN_PROFILE_DIR"
PACKAGED_ENVIRONMENT = "JINGBAN_PACKAGED"


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS).resolve()
    return Path(__file__).resolve().parent.parent


@contextmanager
def launch_profile_environment(profile_dir: Path | None):
    """Workers inherit this launch's profile; ordinary launches use their own default."""
    previous = os.environ.get(PROFILE_ENVIRONMENT)
    previous_packaged = os.environ.get(PACKAGED_ENVIRONMENT)
    if profile_dir is None:
        os.environ.pop(PROFILE_ENVIRONMENT, None)
    else:
        os.environ[PROFILE_ENVIRONMENT] = str(Path(profile_dir).expanduser().resolve())
    if getattr(sys, "frozen", False):
        # The bundled Python workers are ordinary interpreters. Preserve the
        # release safety gate across that process boundary.
        os.environ[PACKAGED_ENVIRONMENT] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(PROFILE_ENVIRONMENT, None)
        else:
            os.environ[PROFILE_ENVIRONMENT] = previous
        if previous_packaged is None:
            os.environ.pop(PACKAGED_ENVIRONMENT, None)
        else:
            os.environ[PACKAGED_ENVIRONMENT] = previous_packaged

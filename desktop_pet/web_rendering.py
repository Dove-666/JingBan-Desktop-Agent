"""Process-local WebEngine compatibility policy, called before Qt starts.

The Windows chat is a mostly static 2D surface. Software Chromium rendering
avoids driver-dependent GPU texture exchange while preserving Qt's own backend.
Set AURA_WEBENGINE_RENDERER=auto before launch to keep Chromium's default path.
No persistent preferences, system environment or registry values are written.
"""

from collections.abc import MutableMapping
import os
import re
import shlex
import sys


def _has_disable_gpu(flags: str) -> bool:
    try:
        # Parse only for detection; never rebuild or reorder the caller's flags.
        tokens = shlex.split(flags, comments=False, posix=True)
    except ValueError:
        # A malformed caller-supplied quote must not crash startup. Preserve it
        # verbatim and still make repeated initialization harmless.
        tokens = re.findall(r"(?<!\S)--disable-gpu(?:=[^\s]*)?(?!\S)", flags)
    return any(token.partition("=")[0] == "--disable-gpu" for token in tokens)


def configure_webengine_rendering(
    environ: MutableMapping[str, str] | None = None,
    platform_name: str | None = None,
) -> str:
    """Select ``software`` or ``auto`` without importing/initializing Qt.

    An explicitly supplied mapping, even an empty one, is used in place of the
    process environment. Existing Chromium switches remain byte-for-byte intact.
    The function is idempotent so script and direct ``app.main`` entry points can
    both call it before the first QApplication/WebEngine initialization.
    """
    values = os.environ if environ is None else environ
    platform = sys.platform if platform_name is None else platform_name
    default = "software" if platform.lower().startswith("win") else "auto"
    requested = values.get("AURA_WEBENGINE_RENDERER", "").strip().lower()
    mode = requested if requested in {"auto", "software"} else default
    if mode == "software":
        flags = values.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        if not _has_disable_gpu(flags):
            separator = "" if not flags or flags[-1].isspace() else " "
            values["QTWEBENGINE_CHROMIUM_FLAGS"] = flags + separator + "--disable-gpu"
    return mode

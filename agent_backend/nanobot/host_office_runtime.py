"""Strict host Office selection and private renderer capability separation.

No IO or process starts at import. Paths arrive only in the host run frame;
model tools cannot choose executables, a profile, Python, or renderer options.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

try:
    from .office_render import OfficeRenderError
    from .libreoffice_render import LibreOfficeRenderAdapter
    from .document_previews import PagePreviewCache
except ImportError:
    from office_render import OfficeRenderError
    from libreoffice_render import LibreOfficeRenderAdapter
    from document_previews import PagePreviewCache

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
_KEYS = frozenset({"engine", "runtime", "scratch_parent", "pdftoppm"})


class HostOfficeConfigurationError(ValueError):
    def __init__(self): super().__init__("HOST_OFFICE_CONFIGURATION_INVALID")


def _absolute(value):
    if (type(value) is not str or not 1 <= len(value) <= 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise HostOfficeConfigurationError()
    path = Path(value)
    if not path.is_absolute() or value.startswith(("\\\\", "//")):
        raise HostOfficeConfigurationError()
    if os.name == "nt" and ":" in value[2:]:
        raise HostOfficeConfigurationError()  # No alternate data streams/device syntax.
    try: return path.resolve()
    except (OSError, ValueError, RuntimeError): raise HostOfficeConfigurationError() from None


def _overlap(left, right):
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def checked_office_renderer(value):
    """Presence opts in. null/unknown/malformed never silently selects COM."""
    if type(value) is not dict or set(value) != _KEYS or value.get("engine") != "libreoffice":
        raise HostOfficeConfigurationError()
    config = {"engine": "libreoffice", **{key: str(_absolute(value[key])) for key in ("runtime", "scratch_parent", "pdftoppm")}}
    scratch = Path(config["scratch_parent"])
    if any(_overlap(scratch, private) for private in _protected(config, include_scratch=False)):
        raise HostOfficeConfigurationError()
    return config


def _protected(config, *, include_scratch=True):
    runtime = Path(config["runtime"]).resolve()
    # DLLs and trusted helper/SDK source are executable authority too. A read
    # grant cannot be used to inspect private task profiles or live source data.
    roots = [runtime, runtime.parent / "runtime-manifest.json",
        Path(config["pdftoppm"]).resolve().parent, Path(sys.executable).resolve().parent.parent,
        HERE, REPO / "desktop_pet/agents"]
    if include_scratch: roots.append(Path(config["scratch_parent"]).resolve())
    return tuple(root.resolve() for root in roots)


def check_office_scopes(config, workspace, grants=()):
    """Check actual host capabilities; never authorize or install a grant."""
    protected = _protected(config)
    capabilities = [_absolute(str(workspace))]
    if type(grants) not in {list, tuple} or len(grants) > 64:
        raise HostOfficeConfigurationError()
    for grant in grants:
        if (type(grant) is not dict or set(grant) != {"path", "access", "directory"}
                or grant.get("access") not in {"read", "write"} or type(grant.get("directory")) is not bool):
            raise HostOfficeConfigurationError()
        capabilities.append(_absolute(grant.get("path")))
    if any(_overlap(capability, private) for capability in capabilities for private in protected):
        raise HostOfficeConfigurationError()


class HostOfficeRuntime:
    """One reusable binding per worker; no fallback on selected-engine failure."""
    def __init__(self, config, files):
        self.config = checked_office_renderer(config)
        self.files = files
        self.binding = None
        self.preview_cache = PagePreviewCache(files)

    def _binding(self):
        self.assert_scopes()
        if self.binding is not None: return self.binding
        # -I imports only the fixed repository root; never a model workspace.
        if str(REPO) not in sys.path:
            if os.environ.get("JINGBAN_PACKAGED") == "1": sys.path.append(str(REPO))
            else: sys.path.insert(0, str(REPO))
        from desktop_pet.agents.libreoffice_renderer import LibreOfficeRenderer
        engine = LibreOfficeRenderer(runtime=self.config["runtime"], python=sys.executable,
            pdftoppm=self.config["pdftoppm"], scratch_parent=self.config["scratch_parent"], enabled=True)
        self.binding = LibreOfficeRenderAdapter(engine, cleanup_reconciler=engine.reconcile_cleanup_evidence)
        return self.binding

    def assert_scopes(self, *, extra_grant=None):
        try:
            with self.files._lock:
                grants = [{"path": str(path), "directory": directory, "access": access}
                    for access, roots in (("read", self.files._read_roots), ("write", self.files._write_roots))
                    for path, directory in roots]
                if extra_grant is not None: grants.append(extra_grant)
                check_office_scopes(self.config, self.files.root, grants)
        except Exception:
            error = OfficeRenderError("OFFICE_ISOLATION_UNAVAILABLE")
            error.safe_no_publication = True
            raise error from None

    async def render_document(self, path, output_path=None, overwrite=False, expected_hash=None):
        return await self._binding().render_document(path, output_path, files=self.files,
            overwrite=overwrite, expected_hash=expected_hash, host_scope_guard=self.assert_scopes,
            preview_cache=self.preview_cache)

    def preview(self, reference):
        """Host-only authorized bytes, never a model JSON/data-URL response."""
        return self.preview_cache.image(reference)

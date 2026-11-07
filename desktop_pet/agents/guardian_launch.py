"""Opaque remote-queue launch capability issued only by the host coordinator."""
from __future__ import annotations

from .job_queue import HostJobReference

_ISSUER = object()


class HostGuardianLaunch:
    __slots__ = ("_issuer", "_job_id", "_reference", "_commit", "_attempt")
    def __new__(cls, *args, **kwargs): raise TypeError("HOST_GUARDIAN_LAUNCH_REQUIRED")
    def __setattr__(self, *args): raise AttributeError("HOST_GUARDIAN_LAUNCH_IMMUTABLE")
    def __reduce__(self): raise TypeError("HOST_GUARDIAN_LAUNCH_NOT_SERIALIZABLE")
    def __repr__(self): return "HostGuardianLaunch([authenticated queued host task])"
    @property
    def job_id(self): return self._job_id
    @property
    def reference(self): return self._reference

    def validate_attempt(self, attempt):
        if self._issuer is not _ISSUER: raise ValueError("HOST_GUARDIAN_LAUNCH_INVALID")
        return self._attempt(attempt)

    def commit_manifest(self, manifest):
        if self._issuer is not _ISSUER: raise ValueError("HOST_GUARDIAN_LAUNCH_INVALID")
        return self._commit(manifest)


def _issue(job_id, reference, commit, attempt):
    if type(reference) is not HostJobReference or reference.origin_peer_id is None or not callable(commit) or not callable(attempt):
        raise ValueError("HOST_GUARDIAN_LAUNCH_INVALID")
    reference.validate()
    value = object.__new__(HostGuardianLaunch)
    for name, item in (("_issuer", _ISSUER), ("_job_id", job_id), ("_reference", reference), ("_commit", commit), ("_attempt", attempt)):
        object.__setattr__(value, name, item)
    return value


def valid(value):
    return type(value) is HostGuardianLaunch and getattr(value, "_issuer", None) is _ISSUER

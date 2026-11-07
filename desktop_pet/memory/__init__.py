"""Optional Mem0 OSS memory, separate from the conversation database."""

__all__ = ["MemoryService", "MemoryServiceProtocol", "MemorySettings", "MemoryUnavailable"]


def __getattr__(name):
    # A worker imports this package before its module. Do not initialize the
    # Qt-backed host/credential layer merely to execute an isolated SDK worker.
    if name == "MemorySettings":
        from .settings import MemorySettings
        return MemorySettings
    if name in {"MemoryService", "MemoryServiceProtocol", "MemoryUnavailable"}:
        from . import service
        return getattr(service, name)
    raise AttributeError(name)

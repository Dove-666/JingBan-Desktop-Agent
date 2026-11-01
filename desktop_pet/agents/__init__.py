"""Optional, lazy Agent backend integration.

Importing this package loads only the standard library. It never imports an
Agent SDK, creates a worker, observes the desktop, or starts a background task.
"""

from .contracts import AgentError, AgentRequest, AgentResult, AgentValidationError
from .settings import (
    AgentSettings, GUI_CREDENTIAL_ACCOUNT, load_agent_settings, save_agent_settings,
)
from .service import AgentService

__all__ = [
    "AgentError", "AgentRequest", "AgentResult", "AgentValidationError",
    "AgentSettings", "AgentService", "GUI_CREDENTIAL_ACCOUNT",
    "load_agent_settings", "save_agent_settings",
]

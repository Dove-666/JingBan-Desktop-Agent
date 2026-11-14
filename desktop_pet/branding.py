"""Public product identity, separate from persistent compatibility identifiers."""

from .runtime_paths import resource_root

APP_NAME = "鲸伴"

# QStandardPaths and single-instance scope must keep resolving existing data.
# This is a storage identifier, not the name presented by the application.
LEGACY_APPLICATION_ID = "AURA Desktop Pet"


def application_icon_path():
    """Use the launcher artwork independently of the animated pet's image."""
    return resource_root() / "assets/branding/windows/whale-maid.ico"

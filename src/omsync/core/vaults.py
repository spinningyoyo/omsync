"""The vault that the `vault` setting names, or None when it is "none"."""

from omsync.core.bitwarden import BitwardenCLI, BitwardenVault
from omsync.core.settings import Settings
from omsync.core.vault import SystemKeychain, Vault


def open_vault(settings: Settings, *, email: str | None = None) -> Vault | None:
    """The configured vault, locked. email replaces the email setting, as the CLI's --email does."""
    if settings.vault == "bitwarden":
        return BitwardenVault(
            BitwardenCLI(appdata_dir=settings.data_dir / "bitwarden"),
            SystemKeychain(),
            settings.vault_idle_minutes,
            email or settings.email,
        )
    return None

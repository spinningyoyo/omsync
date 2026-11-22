"""Vendor connector protocol and base class for browser-driven receipt acquisition."""

import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Protocol

from selenium.common.exceptions import TimeoutException, WebDriverException

from omsync.core.settings import Settings
from omsync.core.vault import Vault
from omsync.ingest.connectors.base import ConnectorError, NeedsUserAction, Prompt
from omsync.ingest.connectors.browser import BrowserSession


class VendorConnectorProtocol(Protocol):
    """The vendor connector protocol."""

    key: str
    label: str
    uses_vault: bool  # whether the connector can sign in with a vault login

    def fetch(
        self,
        settings: Settings,
        prompt: Prompt,
        *,
        provider_key: str,
        held: Mapping[str, Path | None],
        start: date,
        end: date,
        vault: Vault | None = None,
        vault_item_id: str | None = None,
    ) -> list[Path]:
        """Download the provider's receipt files for start..end into its folder.

        held maps the ID of each receipt the provider already holds to the file it was read from.
        Returns the paths of files written to disk.
        """
        ...


class VendorConnector(BrowserSession):
    """Base class for merchant / vendor receipt connectors using Chrome."""

    key: ClassVar[str]
    label: ClassVar[str]
    uses_vault: ClassVar[bool]
    browser_mode: ClassVar[str] = "chrome-attach"

    def fetch(
        self,
        settings: Settings,
        prompt: Prompt,
        *,
        provider_key: str,
        held: Mapping[str, Path | None],
        start: date,
        end: date,
        vault: Vault | None = None,
        vault_item_id: str | None = None,
    ) -> list[Path]:
        """Download the provider's receipt files for start..end into its folder."""
        if start > end:
            raise ValueError("start must not be after end")
        profile_dir = settings.browser_profile_dir / self.key / provider_key
        with tempfile.TemporaryDirectory(
            prefix=f"omsync-{self.key}-", ignore_cleanup_errors=True
        ) as scratch:
            self.prepare(download_dir=Path(scratch), profile_dir=profile_dir)
            failed = True
            try:
                driver = self.get_driver(self.browser_mode)
                written = self.download_receipts(
                    driver,
                    settings,
                    prompt,
                    provider_key=provider_key,
                    held=held,
                    start=start,
                    end=end,
                    vault=vault,
                    vault_item_id=vault_item_id,
                )
                failed = False
                return written
            except TimeoutException:
                raise NeedsUserAction(f"{self.label} did not respond in time; sync again") from None
            except WebDriverException as exc:
                err = type(exc).__name__
                raise ConnectorError(
                    f"the browser stopped while fetching {self.label} receipts ({err})"
                ) from None
            finally:
                if failed:
                    self.leave_open()
                else:
                    self.close()

    def download_receipts(
        self,
        driver: Any,
        settings: Settings,
        prompt: Prompt,
        *,
        provider_key: str,
        held: Mapping[str, Path | None],
        start: date,
        end: date,
        vault: Vault | None = None,
        vault_item_id: str | None = None,
    ) -> list[Path]:
        """Download new receipts using the active WebDriver. Implemented by subclasses."""
        raise NotImplementedError

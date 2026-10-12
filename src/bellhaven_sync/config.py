"""Runtime configuration. Everything comes from environment variables with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Does not override existing env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_BASE_URL = "https://analyst-assessment-production.up.railway.app"
DEFAULT_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"


@dataclass(frozen=True)
class Settings:
    base_url: str = field(
        default_factory=lambda: os.environ.get("BELLHAVEN_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    )
    token: str = field(default_factory=lambda: os.environ.get("BELLHAVEN_CRM_TOKEN", ""))
    db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("BELLHAVEN_DB_PATH", str(PROJECT_ROOT / "data" / "bellhaven_sync.db"))
        )
    )
    # The parent account we reconcile against. Discovered by name at runtime unless
    # BELLHAVEN_PARENT_ACCOUNT_ID is set explicitly.
    parent_account_name: str = field(
        default_factory=lambda: os.environ.get("BELLHAVEN_PARENT_ACCOUNT_NAME", DEFAULT_PARENT_NAME)
    )
    parent_account_id: str = field(default_factory=lambda: os.environ.get("BELLHAVEN_PARENT_ACCOUNT_ID", ""))
    review_host: str = field(default_factory=lambda: os.environ.get("BELLHAVEN_REVIEW_HOST", "127.0.0.1"))
    review_port: int = field(default_factory=lambda: int(os.environ.get("BELLHAVEN_REVIEW_PORT", "8000")))
    reviewer_name: str = field(
        default_factory=lambda: os.environ.get("BELLHAVEN_REVIEWER", os.environ.get("USERNAME", "reviewer"))
    )

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/api/v1"

    @property
    def site_url(self) -> str:
        return self.base_url

    def crm_browser_url(self, account_id: str) -> str:
        return f"{self.base_url}/crm/{self.token}/accounts/{account_id}"


def get_settings() -> Settings:
    return Settings()

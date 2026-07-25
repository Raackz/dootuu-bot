import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass
class MailerConfig:
    """Control bot + Telethon API settings."""

    bot_token: str = ""
    admin_ids: list[int] = field(default_factory=list)
    admin_usernames: list[str] = field(default_factory=list)
    api_id: int = 0
    api_hash: str = ""
    data_dir: Path = field(default_factory=Path)
    db_path: Path = field(default_factory=Path)
    sessions_dir: Path = field(default_factory=Path)

    # Defaults (overridable in DB settings)
    default_cycle_limit: int = 50
    default_cycle_pause_sec: int = 3600
    default_delay_sec: float = 8.0
    # If true — любой, кто открыл бота, может добавлять аккаунты и управлять
    allow_all: bool = True

    def __post_init__(self) -> None:
        self.bot_token = (os.getenv("MAILER_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
        raw = os.getenv("MAILER_ADMIN_IDS") or os.getenv("ADMIN_IDS") or ""
        self.admin_ids = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
        raw_names = os.getenv("MAILER_ADMIN_USERNAMES") or os.getenv("ADMIN_USERNAMES") or ""
        self.admin_usernames = [
            x.strip().lstrip("@").lower() for x in raw_names.split(",") if x.strip()
        ]
        # MAILER_OPEN=true|false — default true (team self-service)
        open_raw = (os.getenv("MAILER_OPEN") or "true").strip().lower()
        self.allow_all = open_raw in ("1", "true", "yes", "on")
        self.api_id = int(os.getenv("TG_API_ID") or os.getenv("API_ID") or "0")
        self.api_hash = (os.getenv("TG_API_HASH") or os.getenv("API_HASH") or "").strip()
        self.data_dir = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data" / "mailer"))
        self.db_path = self.data_dir / "mailer.db"
        self.sessions_dir = self.data_dir / "sessions"
        self.default_cycle_limit = int(os.getenv("MAILER_CYCLE_LIMIT", "50"))
        self.default_cycle_pause_sec = int(os.getenv("MAILER_CYCLE_PAUSE_SEC", "3600"))
        self.default_delay_sec = float(os.getenv("MAILER_DELAY_SEC", "8"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def is_env_admin(self, user_id: int | None, username: str | None = None) -> bool:
        if user_id is not None and user_id in self.admin_ids:
            return True
        if username and username.lstrip("@").lower() in self.admin_usernames:
            return True
        return False

    def is_admin(self, user_id: int | None, username: str | None = None) -> bool:
        """Backward-compatible: env admin OR open mode."""
        if self.allow_all:
            return True
        if self.is_env_admin(user_id, username):
            return True
        # If no admins configured, allow everyone
        if not self.admin_ids and not self.admin_usernames:
            return True
        return False

    @property
    def telethon_ready(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def set_api(self, api_id: int, api_hash: str) -> None:
        self.api_id = int(api_id)
        self.api_hash = (api_hash or "").strip()

    def apply_api_from_values(self, api_id: str | int | None, api_hash: str | None) -> bool:
        """Apply non-empty credentials. Returns True if telethon becomes ready."""
        if api_id is not None and str(api_id).strip().isdigit():
            self.api_id = int(str(api_id).strip())
        if api_hash is not None and str(api_hash).strip():
            self.api_hash = str(api_hash).strip()
        return self.telethon_ready

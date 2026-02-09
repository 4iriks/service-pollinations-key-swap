from dataclasses import dataclass, field
from os import getenv

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    bot_token: str = field(default_factory=lambda: getenv("SERVICE_BOT_TOKEN", ""))
    admin_ids: list[int] = field(default_factory=lambda: [
        int(x.strip()) for x in getenv("ADMIN_IDS", "").split(",") if x.strip()
    ])
    api_host: str = field(default_factory=lambda: getenv("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(getenv("API_PORT", "8080")))
    db_path: str = field(default_factory=lambda: getenv("DB_PATH", "service.db"))
    vless_configs: list[str] = field(default_factory=lambda: [
        u.strip() for u in getenv("VLESS_CONFIGS", "").split(",") if u.strip()
    ])
    balance_threshold: float = field(default_factory=lambda: float(getenv("BALANCE_THRESHOLD", "0.1")))
    balance_check_interval: int = field(default_factory=lambda: int(getenv("BALANCE_CHECK_INTERVAL", "10")))

    def __post_init__(self):
        if not self.bot_token:
            raise ValueError("SERVICE_BOT_TOKEN is required")


settings = Settings()

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    client_id: str
    client_secret: str
    encryption_key: str
    app_url: str = "http://localhost:8000"
    api_version: str = "2026-10"
    old_client_secret: str | None = None

    def __post_init__(self):
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("DATABASE_URL must point to Postgres")
        if not self.client_id or not self.client_secret:
            raise ValueError("Set SHOPIFY_API_KEY and SHOPIFY_API_SECRET for your app")
        Fernet(self.encryption_key.encode())
        url = urlsplit(self.app_url)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("SHOPIFY_APP_URL must use HTTPS (localhost is allowed for development)")
        if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise ValueError("SHOPIFY_APP_URL must be an origin without credentials, path or query")
        if not re.fullmatch(r"20\d{2}-(01|04|07|10)", self.api_version):
            raise ValueError("SHOPIFY_API_VERSION must be a stable version such as 2026-10")

    @classmethod
    def from_env(cls):
        load_dotenv()
        return cls(
            database_url=os.environ["DATABASE_URL"],
            client_id=os.environ["SHOPIFY_API_KEY"],
            client_secret=os.environ["SHOPIFY_API_SECRET"],
            encryption_key=os.environ["TOKEN_ENCRYPTION_KEY"],
            app_url=os.getenv("SHOPIFY_APP_URL", "http://localhost:8000").rstrip("/"),
            api_version=os.getenv("SHOPIFY_API_VERSION", "2026-10"),
            old_client_secret=os.getenv("SHOPIFY_OLD_API_SECRET") or None,
        )

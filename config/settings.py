"""Central config. All secrets come from .env — never hardcoded, never in the sheet."""
import os
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    vlm_model: str = "gpt-4o"
    agent_model: str = "gpt-4o"

    confidence_threshold: float = 0.80

    masjid_directory: str = "data/masjids.xlsx"
    google_service_account_json: str = ""

    portal_login_url: str = "https://masjidal.com/backend/site/login"
    portal_email: str = ""
    portal_password: str = ""
    portal_upload_enabled: bool = False
    portal_headless: bool = True   # set false to WATCH Chrome do the portal upload live

    database_url: str = "sqlite:///data/masjidos.db"
    output_dir: str = "data/outputs"

    @field_validator("database_url")
    @classmethod
    def _normalize_postgres_scheme(cls, v: str) -> str:
        # Render/Heroku-style managed Postgres connection strings use the
        # legacy "postgres://" scheme; SQLAlchemy 1.4+ requires "postgresql://"
        # and raises otherwise. Normalize here so DATABASE_URL can be wired
        # straight from the platform's own database without a manual edit.
        if v.startswith("postgres://"):
            return "postgresql://" + v[len("postgres://"):]
        return v

    # ---- API security ----
    api_key: str = ""             # required X-API-Key header value for app/api.py
    cors_allow_origins: str = "*" # comma-separated; "*" for demo, restrict in real prod

    # ---- Observability ----
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "masjidos"
    log_dir: str = "data/logs"

    @property
    def output_path(self) -> Path:
        p = ROOT / self.output_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_path(self) -> Path:
        p = ROOT / self.log_dir
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()

# Observability, wired here since every entry point (agent/run.py, app/api.py,
# app/review_app.py) imports `settings` first, before building any agent or
# handling any request.
if settings.langchain_tracing_v2:
    # LangChain/LangGraph read tracing config from real process env vars, not
    # from this pydantic-settings object — export them explicitly.
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project

from loguru import logger  # noqa: E402

logger.add(
    settings.log_path / "masjidos.log",
    rotation="10 MB",
    retention="14 days",
    level="INFO",
    enqueue=True,
    backtrace=False,
    diagnose=False,
)

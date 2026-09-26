from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "Invoice Intelligence"
    database_url: str = f"sqlite:///{BASE_DIR / 'invoice_reader.db'}"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_insights_model: str = "gpt-5-mini"
    max_upload_mb: int = 10
    upload_dir: Path = BASE_DIR / "uploads"

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)

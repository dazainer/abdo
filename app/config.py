from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    anthropic_api_key: str
    telegram_bot_token: str
    telegram_webhook_secret: str    # random string used in the webhook URL path
    database_url: str               # set on the app service as a reference: ${{Postgres.DATABASE_URL}}
    cohere_api_key: str | None = None  # Cohere Embed v4 for the household knowledge base (Phase 2)
    # Phase 3 — shared calendar + live location
    google_service_account_json: str | None = None   # the JSON key, as a string
    google_calendar_id: str | None = None
    home_lat: float | None = None
    home_lng: float | None = None
    timezone: str = "Africa/Cairo"


settings = Settings()

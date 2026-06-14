from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    anthropic_api_key: str
    telegram_bot_token: str
    telegram_webhook_secret: str    # random string used in the webhook URL path
    database_url: str               # Railway injects this automatically
    timezone: str = "Africa/Cairo"


settings = Settings()

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
    # Phase 4 — Egyptian voice over Telegram (ElevenLabs: Scribe STT + Flash v2.5 TTS).
    # STT/TTS are isolated in stt.py/tts.py; the Egyptian accent comes from the chosen
    # voice, not the default Arabic voice — audition/clone a Cairene one for tts_voice_id.
    elevenlabs_api_key: str | None = None
    tts_voice_id: str | None = None             # an Egyptian-accent voice (Voice Library or a clone)
    tts_model: str = "eleven_flash_v2_5"        # fastest (~75ms) and half the credit cost
    stt_model: str = "scribe_v2"                # scribe_v1 is deprecated/removed 2026-07-09
    stt_language: str = "ar"                    # Arabic (expect code-switching with English)
    voice_replies: bool = True                  # reply with voice when the user sent voice
    reply_text_alongside_voice: bool = True     # send text first (instant), then the voice note
    max_voice_seconds: int = 60                 # ignore monologues longer than this


settings = Settings()

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Strip whitespace from every value: a secret pasted into Railway with a trailing space
    # or newline otherwise silently fails every comparison (it once 403'd every webhook).
    model_config = SettingsConfigDict(env_file=".env", str_strip_whitespace=True)

    anthropic_api_key: str
    telegram_bot_token: str
    telegram_webhook_secret: str    # random string used in the webhook URL path
    database_url: str               # set on the app service as a reference: ${{Postgres.DATABASE_URL}}
    cohere_api_key: str | None = None  # Cohere Embed v4 for the household knowledge base
    # Shared calendar + live location
    google_service_account_json: str | None = None   # the JSON key, as a string
    google_calendar_id: str | None = None
    home_lat: float | None = None
    home_lng: float | None = None
    timezone: str = "America/Toronto"
    # Egyptian voice (ElevenLabs: Scribe STT + Flash v2.5 TTS).
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
    # Web app (Phase 6). Without SESSION_PEPPER the web login is disabled.
    session_pepper: str | None = None           # random string; hashes login codes + session tokens
    cookie_secure: bool = True                  # set false only for http://localhost dev
    vapid_public_key: str | None = None         # web push (scripts/gen_vapid.py)
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:abdo@example.com"
    # Proactive Abdo (Phase 7a)
    scheduler_enabled: bool = True              # the background loop (off in tests/demo)
    prayer_method: str = "NORTH_AMERICA"        # adhanpy CalculationMethod name (ISNA)
    prayer_asr: str = "SHAFI"                   # SHAFI (standard) or HANAFI
    # Buses (Phase 7b.4): the GraphQL API behind GRT's own departures site (stoptimes.grt.ca).
    # Not a documented open-data API, so it's configurable and every failure is reported, never guessed.
    grt_api_url: str = "https://grtivr-prod.regionofwaterloo.9802690.ca/vms/graphql"
    # Room device (Phase 8a). Without ROOM_DEVICE_TOKEN the room endpoint is off.
    room_device_token: str | None = None
    room_speaker_min_confidence: float = 0.75   # below this, a room speaker is "Someone"


settings = Settings()

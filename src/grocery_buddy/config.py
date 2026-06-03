from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Anthropic
    anthropic_api_key: str = ""
    model_smart: str = "claude-sonnet-4-6"
    model_fast: str = "claude-haiku-4-5-20251001"

    # Supabase / Postgres
    supabase_url: str = ""
    supabase_anon_key: str = ""
    database_url: str = ""

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "grocery-buddy"

    # Langfuse (optional — traces are no-ops when keys are absent)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Public URL where this webhook server is reachable (used only to register
    # the Telegram webhook — the Telegram Bot API calls this URL).
    # For local dev use ngrok: ngrok http 8080
    webhook_base_url: str = "http://localhost:8080"

    # Telegram — all notifications and inbound chat
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""   # your DM with the bot (from getUpdates)

    # Single-user: all inbound Telegram chat messages are attributed to this user
    grocery_buddy_user_id: str = ""

    # Purchase
    auto_purchase_cap_usd: float = 50.0

    # Scheduled-run guardrail: skip a new full grocery run if one already ran
    # for the user within this many minutes (prevents repeated auto-purchases /
    # stacked briefings on a high-frequency cron like */5). Ad-hoc QuickBuys are
    # never blocked by this.
    run_cooldown_minutes: int = 180

    # Amazon automation
    amazon_profile_dir: str = ".amazon-session"
    amazon_headless: bool = True


settings = Settings()

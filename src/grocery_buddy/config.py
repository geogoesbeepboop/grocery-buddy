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

    # ntfy
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = "grocery-buddy"
    webhook_base_url: str = "http://localhost:8080"

    # Purchase
    auto_purchase_cap_usd: float = 50.0

    # Amazon automation
    amazon_profile_dir: str = ".amazon-session"
    amazon_headless: bool = True


settings = Settings()

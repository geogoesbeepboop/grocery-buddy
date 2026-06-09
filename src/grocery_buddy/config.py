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

    # ── Evals, cost, and the money-live readiness gate ────────────────────────
    # Per-run LLM cost (USD) above which check_cost_alert fires an alert. Now fed a
    # REAL number summed from the llm_usage ledger (was a hardcoded 0.0).
    cost_alert_threshold_usd: float = 1.00
    # Prediction-accuracy eval window: snapshots from the last `lookback_days`, each
    # scored against purchases within `horizon_days` of the snapshot.
    eval_lookback_days: int = 14
    eval_horizon_days: int = 7
    # Money-live readiness gate (gating.py). The sandbox auto-buy spine is built
    # behind these; flipping `money_live` on is only honored when every condition
    # passes. Defaults are conservative and money stays OFF until explicitly enabled.
    auto_buy_enabled: bool = False
    money_live: bool = False
    gate_predictor_precision_floor: float = 0.70
    gate_run_cost_ceiling_usd: float = 0.50
    # Conversation transcript cap: keep at most this many messages of the persisted
    # per-user transcript (rolling window) so onboarding/import transcripts can't grow
    # unbounded and get re-sent in full every webhook turn. ~20 turns.
    conversation_max_messages: int = 40

    # Free next-day shipping. Amazon's Prime grocery / add-on free-shipping minimum
    # is ~$25; below it the user may pay a delivery fee. When a grocery run assembles
    # a cart under this, the agent tops it up with the items due to run out soonest
    # (clearly labeled, droppable at approval) so the whole order ships free.
    free_shipping_threshold_usd: float = 25.0
    # Cap on the "added to reach free shipping" fillers — both how many we price
    # (each is a browser search) and how many we'll actually add. Keeps a small run
    # from ballooning into a big speculative order.
    free_shipping_max_fillers: int = 6

    # Rendering: most items to list per stock bucket in /status (and the post-import
    # recap) before collapsing the rest into a "…and N more" line. A 90-item pantry
    # listed in full overflows Telegram's 4096-char message limit. 0 = no cap.
    status_max_items_per_bucket: int = 12

    # Scheduled-run guardrail: skip a new full grocery run if one already ran
    # for the user within this many minutes (prevents repeated auto-purchases /
    # stacked briefings on a high-frequency cron like */5). Ad-hoc QuickBuys are
    # never blocked by this.
    run_cooldown_minutes: int = 180

    # After staging a checkout, how long the workflow keeps a durable ear open for
    # the user's "I placed the order" confirmation before it gives up. On confirm,
    # the items become in-transit and the workflow sleeps until their estimated
    # arrival to top up the pantry. If the user never confirms, we simply never
    # assume the order happened. 72h covers a weekend.
    purchase_confirm_wait_hours: int = 72

    # Amazon automation
    amazon_profile_dir: str = ".amazon-session"
    amazon_headless: bool = True
    # First name of the account's primary shopper. An Amazon account can host
    # several household profiles, so the raw orders list mixes everyone's
    # purchases. We type this into the orders search box to scope the import to
    # this person's orders. Leave blank to import the full (unfiltered) history.
    amazon_account_first_name: str = ""

    # Self-healing re-login. When the saved Amazon session expires, /import logs
    # back in on its own instead of asking you to run a terminal command. Set these
    # to fill credentials automatically (works unattended, even on scheduled runs);
    # leave them blank to have the system open a login window for you to sign in
    # once (the import auto-resumes). Either way, if Amazon asks for a 2FA code we
    # relay the prompt to you over Telegram and wait for your reply.
    amazon_email: str = ""
    amazon_password: str = ""
    # Household profile to select after sign-in ("Who's shopping?"), if your
    # account shows that screen. Falls back to AMAZON_ACCOUNT_FIRST_NAME when blank.
    amazon_profile_name: str = ""
    # How long (seconds) to wait for you to reply with a 2FA code, or to finish an
    # interactive login, before giving up and falling back to manual setup.
    amazon_login_wait_seconds: int = 240
    # Order-history import paging — how deep to read and how politely. Bigger
    # max_orders covers a longer history at the cost of more pages / time.
    amazon_import_max_pages: int = 20
    amazon_import_max_orders: int = 200
    # Order-history synthesis (Sonnet) output budget, in tokens. One proposed
    # pantry entry is ~200 output tokens, so a long history (200+ items) needs far
    # more than a fixed 8k or the proposal truncates mid-JSON and yields an empty
    # pantry. Left at 0 we scale the budget to the product count automatically; set
    # a positive number to pin an explicit ceiling (capped at Sonnet's max output).
    amazon_import_synthesis_max_tokens: int = 0


settings = Settings()

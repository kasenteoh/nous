from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = ""
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    # User-Agent contact string sent on all outbound scraping (homepages, news,
    # VC portfolios). Must include a contact email — many sites block anonymous
    # traffic, and robots.txt etiquette expects an identifiable agent.
    SEC_USER_AGENT: str = ""
    VERCEL_DEPLOY_HOOK_URL: str = ""

    # ---------------------------------------------------------------------------
    # LLM
    # ---------------------------------------------------------------------------

    # All LLM calls go through nous.llm.client.complete_json(), backed by
    # DeepSeek's OpenAI-compatible API. Paid (~$0.27/1M input, $1.10/1M output
    # as of 2026) — this intentionally bypasses the spec's "free tier first"
    # rule because Gemini's free tier (20 RPD) was too low for bulk enrichment.
    # Get a key at https://platform.deepseek.com/api_keys
    DEEPSEEK_API_KEY: str = ""

    # ---------------------------------------------------------------------------
    # M3: auto-create + fuzzy match
    # ---------------------------------------------------------------------------

    # pg_trgm similarity threshold for fuzzy company-name matching during
    # auto-create (VC portfolios, news, TechCrunch). 0.85 is the confirmed M3
    # default; lower it to catch more near-misses, raise it to reduce false
    # positives. Open Question §5 in the M3 plan flags this for revisit after
    # the first monthly refresh produces real near-miss data.
    COMPANY_FUZZY_MATCH_THRESHOLD: float = 0.85

    # ---------------------------------------------------------------------------
    # M5: employee estimation
    # ---------------------------------------------------------------------------

    # GitHub REST API token for the github-org employee-count signal. In CI this
    # is the built-in ``secrets.GITHUB_TOKEN`` (nothing to provision); locally
    # it's optional — an empty token just skips the GitHub source.
    GITHUB_TOKEN: str = ""

    # estimate-employees re-checks a company at most once per this many days
    # (rows whose employee_count_checked_at is more recent are skipped). The CLI
    # --refetch-after-days flag overrides this per run.
    EMPLOYEE_REFETCH_DAYS: int = 90

    # ---------------------------------------------------------------------------
    # Wave 3: description embeddings
    # ---------------------------------------------------------------------------

    # Where fastembed stores its downloaded ONNX model (~130MB one-time). A
    # deterministic path (rather than fastembed's tempdir default) so GitHub
    # Actions can cache it across runs; "~" is expanded at use time. Empty
    # string = let fastembed pick.
    EMBEDDING_CACHE_DIR: str = "~/.cache/fastembed"

    # ---------------------------------------------------------------------------
    # DB size watchdog
    # ---------------------------------------------------------------------------

    # Supabase free tier database limit.
    DB_SIZE_CAP_MB: int = 500

    # Warn when DB usage reaches this percentage of the cap (0–100).
    DB_SIZE_WARN_PCT: int = 80

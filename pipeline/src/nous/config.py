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

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = ""
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    GEMINI_API_KEY: str = ""
    SEC_USER_AGENT: str = ""
    VERCEL_DEPLOY_HOOK_URL: str = ""

    # ---------------------------------------------------------------------------
    # LLM provider selection
    # ---------------------------------------------------------------------------

    # Selects the backend in nous.llm.client.complete_json().
    # - "gemini" (default): google-genai. Free tier = 20 requests/day per
    #   project on gemini-2.5-flash. Hits the wall fast on bulk enrichment.
    # - "deepseek": OpenAI-compatible API at api.deepseek.com. Paid
    #   (~$0.27/1M input, $1.10/1M output as of 2026). Use when you need
    #   throughput beyond Gemini's free tier — flag explicitly that this
    #   breaks the spec rule "free tier first."
    LLM_PROVIDER: Literal["gemini", "deepseek"] = "gemini"

    # Required when LLM_PROVIDER="deepseek". Get one at
    # https://platform.deepseek.com/api_keys
    DEEPSEEK_API_KEY: str = ""

    # ---------------------------------------------------------------------------
    # ingest-filings settings
    # ---------------------------------------------------------------------------

    # Industry groups to retain from Form D filings. Values must match the
    # `industryGroupType` enum in the Form D XSD exactly (case-sensitive):
    # "Computers", "Other Technology", "Telecommunications" are the three
    # software-adjacent buckets. The spec §3.1 referred to these with a
    # "Technology - " prefix; that's the EDGAR UI label, not the XML value.
    # When overriding via env var, supply a JSON array string, e.g.:
    #   INDUSTRY_GROUPS='["Computers","Other Technology"]'
    INDUSTRY_GROUPS: list[str] = [
        "Computers",
        "Other Technology",
        "Telecommunications",
    ]

    # Extra days to look back beyond the requested window to catch late-filed
    # amendments and guard against EDGAR indexing delays.
    EDGAR_OVERLAP_DAYS: int = 14

    # Maximum EDGAR requests per second.  SEC's stated ceiling is 10 req/s;
    # we default to 5.0 to stay comfortably below it.
    EDGAR_REQUESTS_PER_SECOND: float = 5.0

    # ---------------------------------------------------------------------------
    # M3: auto-create + fuzzy match
    # ---------------------------------------------------------------------------

    # pg_trgm similarity threshold for fuzzy company-name matching during
    # auto-create (VC portfolios, news, TechCrunch). 0.85 is the confirmed M3
    # default; lower it to catch more near-misses, raise it to reduce false
    # positives. Open Question §5 in the M3 plan flags this for revisit after
    # the first monthly refresh produces real near-miss data.
    COMPANY_FUZZY_MATCH_THRESHOLD: float = 0.85

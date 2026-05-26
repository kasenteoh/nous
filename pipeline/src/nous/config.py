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

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

    # Industry groups to retain from Form D filings.
    # When overriding via env var, supply a JSON array string, e.g.:
    #   INDUSTRY_GROUPS='["Technology - Computers","Technology - Other"]'
    # pydantic-settings parses JSON arrays automatically for list[str] fields.
    INDUSTRY_GROUPS: list[str] = [
        "Technology - Computers",
        "Technology - Other",
        "Technology - Telecommunications",
    ]

    # Extra days to look back beyond the requested window to catch late-filed
    # amendments and guard against EDGAR indexing delays.
    EDGAR_OVERLAP_DAYS: int = 14

    # Maximum EDGAR requests per second.  SEC's stated ceiling is 10 req/s;
    # we default to 5.0 to stay comfortably below it.
    EDGAR_REQUESTS_PER_SECOND: float = 5.0

# DATABASE_URL must use the postgresql+psycopg:// scheme (psycopg 3 async driver).
# Example: postgresql+psycopg://user:pass@localhost:5432/dbname

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nous.config import Settings

# Module-level cached engine. Initialized lazily on first call to get_engine()
# so that importing this module does not fail when DATABASE_URL is unset.
_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Construct (or return the cached) async engine from Settings.DATABASE_URL.

    Connection guards (runs 27425089917 / 27436088686 each lost an hour of
    enrich to one wedged pooler connection — pipeline stages idle the
    connection for 60-120s during LLM calls, NAT drops it silently, and the
    next statement hangs in TCP retransmit):

    - TCP keepalives surface a dead socket in ~60s instead of ~15 minutes.
    - pool_pre_ping discards dead pooled connections at checkout.
    - statement_timeout=60s aborts any single statement server-side, so a
      lock wait or stuck pooler raises instead of stalling a stage. Alembic
      migrations are unaffected (alembic/env.py builds its own engine).

    A guard tripping crashes the stage loudly — by design. Stages are
    idempotent and resumable; the next scheduled run picks up where this one
    stopped, which beats silently burning a workflow step's whole timeout.
    """
    global _engine
    if _engine is None:
        settings = Settings()
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                "options": "-c statement_timeout=60000",
            },
        )
    return _engine


def _make_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the lazy engine.

    This wrapper is called lazily inside AsyncSessionLocal so that importing
    this module does not immediately attempt to parse DATABASE_URL.
    """
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False)


class _LazySessionMaker:
    """Thin proxy that creates the real async_sessionmaker on first use.

    Avoids creating the engine (and thus validating DATABASE_URL) at import
    time, which would fail in environments where DATABASE_URL is not set.
    """

    _factory: async_sessionmaker[AsyncSession] | None = None

    def _get(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            self._factory = _make_session_factory()
        return self._factory

    def __call__(self) -> AsyncSession:
        return self._get()()

    # Support `async with AsyncSessionLocal() as session` — delegate to factory
    def __aenter__(self) -> object:
        return self._get().__aenter__()  # type: ignore[attr-defined]


AsyncSessionLocal: _LazySessionMaker = _LazySessionMaker()


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession, committing on success and rolling back on exception."""
    factory = _make_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

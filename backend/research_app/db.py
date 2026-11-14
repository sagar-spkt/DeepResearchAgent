from pathlib import Path

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from .config import Settings
from .models import Base, Event, SchemaVersion, WorkerLock


def create_session_factory(settings: Settings):
    if settings.database_url.startswith("sqlite:///./"):
        Path(settings.database_url.removeprefix("sqlite:///")).parent.mkdir(
            parents=True, exist_ok=True
        )
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False, "timeout": 30}
        if settings.database_url.startswith("sqlite")
        else {},
        pool_pre_ping=True,
    )
    if settings.database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def pragmas(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

    return sessionmaker(bind=engine, expire_on_commit=False)


def migrate(factory):
    """Initial explicit schema migration; subsequent versions must add migration steps."""
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.execute(
                text("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_vector vector(384)")
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding_vector vector_cosine_ops)"
                )
            )
    with factory.begin() as session:
        version = session.scalar(select(SchemaVersion.version))
        if version is not None and version != 1:
            raise RuntimeError("Unsupported database schema version")
        if version is None:
            session.add(SchemaVersion(version=1))
        if session.get(WorkerLock, 1) is None:
            session.add(WorkerLock(id=1))


def require_schema(factory):
    try:
        with factory() as session:
            if session.scalar(select(SchemaVersion.version)) != 1:
                raise RuntimeError("Database migration required")
    except Exception as exc:
        raise RuntimeError(
            "Run python -m research_app.manage migrate before starting the service"
        ) from exc


def add_event(session, run_id, kind, data):
    event = Event(run_id=run_id, kind=kind, data=data)
    session.add(event)
    session.flush()
    return event

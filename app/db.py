from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)

if engine.dialect.name == "sqlite":
    # pysqlite's own legacy transactional emulation does not compose with
    # SQLAlchemy's SAVEPOINTs: by default it silently ends the ambient
    # transaction around a `RELEASE SAVEPOINT` (as issued when a `with
    # session.begin_nested():` block exits normally), so a later
    # `session.rollback()` on the *outer* transaction has nothing left to
    # undo and the nested block's writes are left permanently committed --
    # exactly the atomicity this application depends on throughout
    # (`persist_financial_snapshot`, `execute_integrity_run`, `_persist_finding`,
    # ...). Production always runs PostgreSQL via psycopg (see
    # `app.config.Settings.database_url`'s default and `compose.yaml`), which
    # has no such quirk; this only matters for the SQLite database this test
    # suite and any non-Docker local run use. The event pair below is
    # SQLAlchemy's own documented remedy: disable pysqlite's implicit
    # transaction handling entirely and let SQLAlchemy drive `BEGIN`/
    # `SAVEPOINT`/`COMMIT`/`ROLLBACK` explicitly, matching PostgreSQL's
    # semantics. https://docs.sqlalchemy.org/en/20/dialects/sqlite.html
    # ("Serializable isolation / Savepoints / Transactional DDL").
    @event.listens_for(engine, "connect")
    def _sqlite_disable_pysqlite_transaction_control(dbapi_connection, _connection_record) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _sqlite_emit_explicit_begin(conn) -> None:
        conn.exec_driver_sql("BEGIN")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

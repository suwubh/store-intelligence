from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    Boolean, DateTime, Text, Index, event
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./store_intelligence.db")

# SQLite performance pragmas
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def build_engine(url: str = DATABASE_URL):
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(engine, "connect", set_sqlite_pragma)
    else:
        engine = create_engine(url, pool_pre_ping=True)
    return engine


engine = build_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class EventRecord(Base):
    __tablename__ = "events"

    event_id       = Column(String, primary_key=True)
    store_id       = Column(String, nullable=False, index=True)
    camera_id      = Column(String, nullable=False)
    visitor_id     = Column(String, nullable=False, index=True)
    event_type     = Column(String, nullable=False)
    timestamp      = Column(DateTime, nullable=False, index=True)
    zone_id        = Column(String, nullable=True)
    dwell_ms       = Column(Integer, default=0)
    is_staff       = Column(Boolean, default=False)
    confidence     = Column(Float, nullable=False)
    queue_depth    = Column(Integer, nullable=True)
    sku_zone       = Column(String, nullable=True)
    session_seq    = Column(Integer, default=1)

    __table_args__ = (
        Index("ix_store_ts", "store_id", "timestamp"),
        Index("ix_store_visitor", "store_id", "visitor_id"),
        Index("ix_store_event_type", "store_id", "event_type"),
    )


class POSTransaction(Base):
    __tablename__ = "pos_transactions"

    transaction_id  = Column(String, primary_key=True)
    store_id        = Column(String, nullable=False, index=True)
    timestamp       = Column(DateTime, nullable=False, index=True)
    basket_value    = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_pos_store_ts", "store_id", "timestamp"),
    )


class SessionRecord(Base):
    """Materialised visitor sessions — rebuilt on ingest for fast funnel queries."""
    __tablename__ = "sessions"

    session_key     = Column(String, primary_key=True)  # store_id:visitor_id
    store_id        = Column(String, nullable=False, index=True)
    visitor_id      = Column(String, nullable=False)
    entry_time      = Column(DateTime, nullable=True)
    exit_time       = Column(DateTime, nullable=True)
    is_staff        = Column(Boolean, default=False)
    visited_billing = Column(Boolean, default=False)
    converted       = Column(Boolean, default=False)
    reentry_count   = Column(Integer, default=0)
    zones_visited   = Column(Text, default="")   # comma-separated zone ids


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

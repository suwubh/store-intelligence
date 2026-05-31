# PROMPT: Create shared pytest fixtures for database setup, API client configuration, event generation, and event ingestion helpers.
# CHANGES MADE: Ensured fixture isolation, cleaned up sessions after tests, and reset dependency overrides.
import pytest
import uuid
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db

TEST_DB_URL = "sqlite://"
STORE = "ST1008"
NOW = datetime.now(timezone.utc)

@pytest.fixture(scope="function")
def db_engine():
    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)

@pytest.fixture(scope="function")
def db_session(db_engine):
    TestingSession = sessionmaker(bind=db_engine)
    session = TestingSession()
    yield session
    session.close()

@pytest.fixture(scope="function")
def client(db_session):
    def override_get_db():
        yield db_session
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()

@pytest.fixture(scope="function")
def make_event_helper():
    def _make(
        event_type="ENTRY",
        visitor_id=None,
        is_staff=False,
        zone_id=None,
        dwell_ms=0,
        confidence=0.90,
        timestamp=None,
        store_id=STORE,
        session_seq=1,
        queue_depth=None,
    ):
        return {
            "event_id": str(uuid.uuid4()),
            "store_id": store_id,
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
            "event_type": event_type,
            "timestamp": (timestamp or NOW).isoformat(),
            "zone_id": zone_id,
            "dwell_ms": dwell_ms,
            "is_staff": is_staff,
            "confidence": confidence,
            "metadata": {
                "queue_depth": queue_depth,
                "sku_zone": zone_id,
                "session_seq": session_seq,
            },
        }
    return _make

@pytest.fixture(scope="function")
def ingest_helper():
    def _ingest(client, events):
        return client.post("/events/ingest", json={"events": events})
    return _ingest

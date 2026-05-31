# PROMPT: Write a comprehensive pytest test suite for a FastAPI store analytics API.
# The API has endpoints: POST /events/ingest, GET /stores/{id}/metrics,
# GET /stores/{id}/funnel, GET /stores/{id}/heatmap, GET /stores/{id}/anomalies,
# GET /health. Tests must cover: happy path, edge cases (zero traffic, all-staff,
# re-entry deduplication, zero purchases), idempotency on ingest,
# partial success on malformed events, and schema validation failures.
# Use pytest fixtures, TestClient from fastapi.testclient, and in-memory SQLite.
#
# CHANGES MADE: Added explicit tests for re-entry not double-counting in funnel,
# empty store 200 response (not null/crash), idempotency verified by calling twice
# and checking duplicate count, added all-staff clip scenario, added conversion
# rate zero-division guard test.

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db, POSTransaction


# ── Test DB fixture ────────────────────────────────────────────────────────────
# StaticPool is REQUIRED for in-memory SQLite in tests — without it, each new
# connection gets a fresh empty DB and sees "no such table".
TEST_DB_URL = "sqlite://"

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


# ── Event factories ────────────────────────────────────────────────────────────
STORE = "ST1008"   # actual Brigade Bangalore store ID
NOW = datetime.now(timezone.utc)


def make_event(
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


def ingest(client, events):
    return client.post("/events/ingest", json={"events": events})


# ── /events/ingest ─────────────────────────────────────────────────────────────
class TestIngest:
    def test_happy_path(self, client):
        r = ingest(client, [make_event()])
        assert r.status_code == 200
        data = r.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0
        assert data["duplicate"] == 0

    def test_idempotency(self, client):
        """Same payload ingested twice — second call returns duplicate count."""
        event = make_event()
        r1 = ingest(client, [event])
        assert r1.json()["accepted"] == 1

        r2 = ingest(client, [event])
        assert r2.status_code == 200
        assert r2.json()["duplicate"] == 1
        assert r2.json()["accepted"] == 0

    def test_partial_success_on_malformed(self, client):
        good = make_event()
        bad = {**make_event(), "confidence": 5.0}  # confidence > 1.0 — invalid
        r = ingest(client, [good, bad])
        assert r.status_code == 200
        data = r.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 1
        assert len(data["errors"]) == 1

    def test_batch_limit_500(self, client):
        events = [make_event() for _ in range(501)]
        r = ingest(client, events)
        assert r.status_code == 422  # pydantic max_length violation

    def test_empty_batch(self, client):
        r = ingest(client, [])
        assert r.status_code == 200
        assert r.json()["accepted"] == 0


# ── /stores/{id}/metrics ───────────────────────────────────────────────────────
class TestMetrics:
    def test_zero_traffic_returns_200_not_null(self, client):
        """Empty store must return valid metrics, not crash or return null."""
        r = client.get(f"/stores/{STORE}/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["current_queue_depth"] == 0
        assert data["abandonment_rate"] == 0.0

    def test_staff_excluded_from_metrics(self, client):
        """Staff events must not appear in unique_visitors count."""
        staff_event = make_event(event_type="ENTRY", is_staff=True)
        customer_event = make_event(event_type="ENTRY", is_staff=False)
        ingest(client, [staff_event, customer_event])

        r = client.get(f"/stores/{STORE}/metrics")
        assert r.status_code == 200
        assert r.json()["unique_visitors"] == 1

    def test_conversion_rate_zero_purchases(self, client):
        """No POS data → conversion_rate must be 0.0, not error."""
        ingest(client, [make_event(event_type="ENTRY")])
        r = client.get(f"/stores/{STORE}/metrics")
        assert r.status_code == 200
        assert r.json()["conversion_rate"] == 0.0

    def test_queue_depth_from_latest_event(self, client):
        vis = f"VIS_{uuid.uuid4().hex[:6]}"
        ingest(client, [
            make_event(event_type="BILLING_QUEUE_JOIN", visitor_id=vis,
                       zone_id="BILLING", queue_depth=3)
        ])
        r = client.get(f"/stores/{STORE}/metrics")
        assert r.status_code == 200
        assert r.json()["current_queue_depth"] == 3


# ── /stores/{id}/funnel ────────────────────────────────────────────────────────
class TestFunnel:
    def test_funnel_structure(self, client):
        r = client.get(f"/stores/{STORE}/funnel")
        assert r.status_code == 200
        data = r.json()
        stages = [s["stage"] for s in data["stages"]]
        assert stages == ["Entry", "Zone Visit", "Billing Queue", "Purchase"]

    def test_reentry_does_not_double_count(self, client):
        """A visitor who re-enters must count as 1 unique visitor in funnel."""
        vis = f"VIS_{uuid.uuid4().hex[:6]}"
        events = [
            make_event(event_type="ENTRY", visitor_id=vis),
            make_event(event_type="EXIT", visitor_id=vis),
            make_event(event_type="REENTRY", visitor_id=vis),
        ]
        ingest(client, events)

        r = client.get(f"/stores/{STORE}/funnel")
        assert r.status_code == 200
        entry_stage = r.json()["stages"][0]
        assert entry_stage["count"] == 1  # not 2

    def test_all_staff_clip_funnel(self, client):
        """All-staff events → funnel entry count is 0."""
        events = [make_event(event_type="ENTRY", is_staff=True) for _ in range(5)]
        ingest(client, events)
        r = client.get(f"/stores/{STORE}/funnel")
        assert r.status_code == 200
        assert r.json()["stages"][0]["count"] == 0

    def test_drop_off_calculation(self, client):
        """5 enter, 3 visit a zone, 1 reaches billing → correct drop-off %."""
        visitors = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(5)]
        events = [make_event(event_type="ENTRY", visitor_id=v) for v in visitors]
        # 3 visit a zone
        for v in visitors[:3]:
            events.append(make_event(event_type="ZONE_ENTER", visitor_id=v, zone_id="SKINCARE"))
        # 1 reaches billing
        events.append(make_event(event_type="ZONE_ENTER", visitor_id=visitors[0], zone_id="BILLING"))

        ingest(client, events)
        r = client.get(f"/stores/{STORE}/funnel")
        assert r.status_code == 200
        stages = r.json()["stages"]
        assert stages[0]["count"] == 5
        assert stages[1]["count"] == 3
        assert stages[2]["count"] == 1


# ── /stores/{id}/heatmap ──────────────────────────────────────────────────────
class TestHeatmap:
    def test_empty_heatmap(self, client):
        r = client.get(f"/stores/{STORE}/heatmap")
        assert r.status_code == 200
        assert r.json()["zones"] == []

    def test_normalised_score_0_to_100(self, client):
        events = []
        vis = f"VIS_{uuid.uuid4().hex[:6]}"
        ingest(client, [make_event(event_type="ENTRY", visitor_id=vis)])
        for zone in ["SKINCARE", "LIPSTICK", "HAIRCARE"]:
            for _ in range(3 if zone == "SKINCARE" else 1):
                events.append(make_event(event_type="ZONE_ENTER", visitor_id=vis, zone_id=zone))
        ingest(client, events)

        r = client.get(f"/stores/{STORE}/heatmap")
        assert r.status_code == 200
        scores = [z["normalised_score"] for z in r.json()["zones"]]
        assert max(scores) == 100.0
        assert all(0 <= s <= 100 for s in scores)

    def test_data_confidence_flag(self, client):
        """Fewer than 20 sessions → data_confidence should be False."""
        ingest(client, [make_event(event_type="ZONE_ENTER", zone_id="SKINCARE")])
        r = client.get(f"/stores/{STORE}/heatmap")
        assert r.status_code == 200
        for zone in r.json()["zones"]:
            assert zone["data_confidence"] == False


# ── /stores/{id}/anomalies ─────────────────────────────────────────────────────
class TestAnomalies:
    def test_no_anomalies_on_empty_store(self, client):
        r = client.get(f"/stores/{STORE}/anomalies")
        assert r.status_code == 200
        assert r.json()["anomalies"] == []

    def test_queue_spike_warn(self, client):
        ingest(client, [
            make_event(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=7)
        ])
        r = client.get(f"/stores/{STORE}/anomalies")
        assert r.status_code == 200
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" in types

    def test_queue_spike_critical(self, client):
        ingest(client, [
            make_event(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=12)
        ])
        r = client.get(f"/stores/{STORE}/anomalies")
        crits = [a for a in r.json()["anomalies"] if a["severity"] == "CRITICAL"]
        assert len(crits) >= 1

    def test_anomaly_has_suggested_action(self, client):
        ingest(client, [
            make_event(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=8)
        ])
        r = client.get(f"/stores/{STORE}/anomalies")
        for anomaly in r.json()["anomalies"]:
            assert anomaly.get("suggested_action"), "Every anomaly must have a suggested_action"


# ── /health ────────────────────────────────────────────────────────────────────
class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert "service_uptime_seconds" in data

    def test_stale_feed_detected(self, client, db_session):
        """Inject an event with old timestamp — should trigger STALE_FEED."""
        old_ts = (NOW - timedelta(minutes=15)).isoformat()
        ingest(client, [make_event(timestamp=NOW - timedelta(minutes=15))])
        r = client.get("/health")
        assert r.status_code == 200
        for store in r.json()["stores"]:
            if store["store_id"] == STORE:
                assert store["stale_feed"] == True

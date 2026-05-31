# PROMPT: Generate unit tests for the ingestion and health endpoints of a FastAPI retail intelligence API. Test happy paths, batch size limit constraints of 500, partial ingestion errors on malformed schemas, and stale feed alerts on health monitoring when last event lag exceeds 10 minutes.
# CHANGES MADE: Extracted common fixtures to conftest.py. Handled mock datetime offsets to simulate event staleness correctly.

import pytest
from datetime import datetime, timedelta, timezone


class TestIngest:
    def test_happy_path(self, client, make_event_helper, ingest_helper):
        r = ingest_helper(client, [make_event_helper()])
        assert r.status_code == 200
        data = r.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0
        assert data["duplicate"] == 0

    def test_idempotency(self, client, make_event_helper, ingest_helper):
        event = make_event_helper()
        r1 = ingest_helper(client, [event])
        assert r1.json()["accepted"] == 1

        r2 = ingest_helper(client, [event])
        assert r2.status_code == 200
        assert r2.json()["duplicate"] == 1
        assert r2.json()["accepted"] == 0

    def test_partial_success_on_malformed(self, client, make_event_helper, ingest_helper):
        good = make_event_helper()
        bad = {**make_event_helper(), "confidence": 5.0}
        r = ingest_helper(client, [good, bad])
        assert r.status_code == 200
        data = r.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 1
        assert len(data["errors"]) == 1

    def test_batch_limit_500(self, client, make_event_helper, ingest_helper):
        events = [make_event_helper() for _ in range(501)]
        r = ingest_helper(client, events)
        assert r.status_code == 422

    def test_empty_batch(self, client, ingest_helper):
        r = ingest_helper(client, [])
        assert r.status_code == 200
        assert r.json()["accepted"] == 0


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert "service_uptime_seconds" in data

    def test_stale_feed_detected(self, client, make_event_helper, ingest_helper):
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(minutes=15)
        ingest_helper(client, [make_event_helper(timestamp=old_time)])
        r = client.get("/health")
        assert r.status_code == 200
        for store in r.json()["stores"]:
            if store["store_id"] == "ST1008":
                assert store["stale_feed"] == True

# PROMPT: Generate unit tests for the ingestion and health endpoints of a FastAPI retail intelligence API. Test happy paths, batch size limit constraints of 500, partial ingestion errors on malformed schemas, and stale feed alerts on health monitoring when last event lag exceeds 10 minutes.
# CHANGES MADE: Extracted common fixtures to conftest.py. Handled mock datetime offsets to simulate event staleness correctly.

import pytest
from datetime import datetime, timedelta, timezone
from app.database import POSTransaction
from app.ingestion import load_pos_transactions


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

    def test_updated_sample_entry_schema_ingests_and_dedupes(self, client, ingest_helper):
        event = {
            "event_type": "entry",
            "id_token": "ID_60001",
            "store_code": "store_1076",
            "camera_id": "cam1",
            "event_timestamp": "2026-03-08T18:10:05.120000",
            "is_staff": False,
            "gender_pred": "F",
            "age_pred": 28,
            "age_bucket": "25-34",
            "is_face_hidden": False,
            "group_id": None,
            "group_size": None,
        }

        first = ingest_helper(client, [event])
        assert first.status_code == 200
        assert first.json()["accepted"] == 1

        second = ingest_helper(client, [event])
        assert second.status_code == 200
        assert second.json()["duplicate"] == 1

        metrics = client.get("/stores/ST1076/metrics")
        assert metrics.status_code == 200
        assert metrics.json()["unique_visitors"] >= 1

    def test_sample_track_ids_link_to_entry_tokens(self, client, ingest_helper):
        """Full sample file: track_id rows must not inflate visitor count vs id_tokens."""
        from pathlib import Path
        import json

        path = Path("dataset/events/sample_events.jsonl")
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        r = ingest_helper(client, events)
        assert r.status_code == 200
        assert r.json()["accepted"] == len(events)

        funnel = client.get("/stores/ST1076/funnel").json()
        assert funnel["stages"][0]["count"] == 3

    def test_updated_queue_schema_counts_billing_stage(self, client, ingest_helper):
        event = {
            "queue_event_id": "cfd8e3c5-7aa0-4ea3-9b59-692d50da8308",
            "event_type": "queue_completed",
            "track_id": 102,
            "store_id": "ST1076",
            "camera_id": "PURPLLE_MUM_1076_CAM6",
            "zone_id": "PURPLLE_MUM_1076_Z_BILLING_01",
            "zone_name": "Billing Counter Queue",
            "zone_type": "BILLING",
            "is_revenue_zone": "Yes",
            "queue_join_ts": "2026-03-08T18:13:05.080000",
            "queue_served_ts": "2026-03-08T18:13:13.240000",
            "queue_exit_ts": "2026-03-08T18:15:31.840000",
            "wait_seconds": 8,
            "queue_position_at_join": 2,
            "abandoned": False,
        }

        r = ingest_helper(client, [event])
        assert r.status_code == 200
        assert r.json()["accepted"] == 1

        funnel = client.get("/stores/ST1076/funnel").json()
        billing = next(stage for stage in funnel["stages"] if stage["stage"] == "Billing Queue")
        assert billing["count"] == 1

    def test_store_folder_alias_normalizes_to_pos_store(self, client, make_event_helper, ingest_helper):
        event = make_event_helper(store_id="ST_STORE_1")
        r = ingest_helper(client, [event])
        assert r.status_code == 200
        assert r.json()["accepted"] == 1

        metrics = client.get("/stores/ST1008/metrics")
        assert metrics.status_code == 200
        assert metrics.json()["unique_visitors"] == 1

        alias_metrics = client.get("/stores/ST_STORE_1/metrics")
        assert alias_metrics.status_code == 200
        assert alias_metrics.json()["store_id"] == "ST1008"
        assert alias_metrics.json()["unique_visitors"] == 1

    def test_cross_camera_zone_event_links_to_active_entry_session(self, client, make_event_helper, ingest_helper):
        entry = make_event_helper(
            store_id="ST1008",
            camera_id="CAM_ENTRY_01",
            visitor_id="VIS_ENTRY_A",
            event_type="ENTRY",
        )
        floor = make_event_helper(
            store_id="ST1008",
            camera_id="CAM_FLOOR_01",
            visitor_id="VIS_LOCAL_7",
            event_type="ZONE_ENTER",
            zone_id="SKINCARE",
        )

        r = ingest_helper(client, [entry, floor])
        assert r.status_code == 200
        assert r.json()["accepted"] == 2

        funnel = client.get("/stores/ST1008/funnel").json()
        assert funnel["stages"][0]["count"] == 1
        assert funnel["stages"][1]["count"] == 1

    def test_updated_pos_order_file_loads(self, tmp_path, db_session):
        pos_file = tmp_path / "pos.csv"
        pos_file.write_text(
            "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
            "ORD1,10-04-2026,12:15:05,ST1008,399945,Faces Canada,302.33\n"
            "ORD1,10-04-2026,12:15:05,ST1008,353621,Faces Canada,491.77\n",
            encoding="utf-8",
        )

        load_pos_transactions(str(pos_file), db_session)
        txn = db_session.get(POSTransaction, "ORD1")
        assert txn is not None
        assert round(txn.basket_value, 2) == 794.10


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

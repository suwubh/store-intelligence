# PROMPT: Add tests for metrics, funnel, and heatmap calculations, including visitor counting, conversion rates, queue depth tracking, funnel progression, heatmap scoring, and confidence thresholds.
# CHANGES MADE: Extracted common helpers into conftest.py, switched to UUID-based test IDs, and added explicit funnel order assertions.
import pytest
import uuid

class TestMetrics:
    def test_zero_traffic_returns_200_not_null(self, client):
        r = client.get("/stores/ST1008/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["current_queue_depth"] == 0
        assert data["abandonment_rate"] == 0.0

    def test_staff_excluded_from_metrics(self, client, make_event_helper, ingest_helper):
        staff_event = make_event_helper(event_type="ENTRY", is_staff=True)
        customer_event = make_event_helper(event_type="ENTRY", is_staff=False)
        ingest_helper(client, [staff_event, customer_event])

        r = client.get("/stores/ST1008/metrics")
        assert r.status_code == 200
        assert r.json()["unique_visitors"] == 1

    def test_conversion_rate_zero_purchases(self, client, make_event_helper, ingest_helper):
        ingest_helper(client, [make_event_helper(event_type="ENTRY")])
        r = client.get("/stores/ST1008/metrics")
        assert r.status_code == 200
        assert r.json()["conversion_rate"] == 0.0

    def test_queue_depth_from_latest_event(self, client, make_event_helper, ingest_helper):
        vis = f"VIS_{uuid.uuid4().hex[:6]}"
        ingest_helper(client, [
            make_event_helper(event_type="BILLING_QUEUE_JOIN", visitor_id=vis,
                              zone_id="BILLING", queue_depth=3)
        ])
        r = client.get("/stores/ST1008/metrics")
        assert r.status_code == 200
        assert r.json()["current_queue_depth"] == 3


class TestFunnel:
    def test_funnel_structure(self, client):
        r = client.get("/stores/ST1008/funnel")
        assert r.status_code == 200
        data = r.json()
        stages = [s["stage"] for s in data["stages"]]
        assert stages == ["Entry", "Zone Visit", "Billing Queue", "Purchase"]

    def test_reentry_does_not_double_count(self, client, make_event_helper, ingest_helper):
        vis = f"VIS_{uuid.uuid4().hex[:6]}"
        events = [
            make_event_helper(event_type="ENTRY", visitor_id=vis),
            make_event_helper(event_type="EXIT", visitor_id=vis),
            make_event_helper(event_type="REENTRY", visitor_id=vis),
        ]
        ingest_helper(client, events)

        r = client.get("/stores/ST1008/funnel")
        assert r.status_code == 200
        entry_stage = r.json()["stages"][0]
        assert entry_stage["count"] == 1

    def test_all_staff_clip_funnel(self, client, make_event_helper, ingest_helper):
        events = [make_event_helper(event_type="ENTRY", is_staff=True) for _ in range(5)]
        ingest_helper(client, events)
        r = client.get("/stores/ST1008/funnel")
        assert r.status_code == 200
        assert r.json()["stages"][0]["count"] == 0

    def test_drop_off_calculation(self, client, make_event_helper, ingest_helper):
        visitors = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(5)]
        events = [make_event_helper(event_type="ENTRY", visitor_id=v) for v in visitors]
        for v in visitors[:3]:
            events.append(make_event_helper(event_type="ZONE_ENTER", visitor_id=v, zone_id="SKINCARE"))
        events.append(make_event_helper(event_type="ZONE_ENTER", visitor_id=visitors[0], zone_id="BILLING"))

        ingest_helper(client, events)
        r = client.get("/stores/ST1008/funnel")
        assert r.status_code == 200
        stages = r.json()["stages"]
        assert stages[0]["count"] == 5
        assert stages[1]["count"] == 3
        assert stages[2]["count"] == 1


class TestHeatmap:
    def test_empty_heatmap(self, client):
        r = client.get("/stores/ST1008/heatmap")
        assert r.status_code == 200
        assert r.json()["zones"] == []

    def test_normalised_score_0_to_100(self, client, make_event_helper, ingest_helper):
        events = []
        vis = f"VIS_{uuid.uuid4().hex[:6]}"
        ingest_helper(client, [make_event_helper(event_type="ENTRY", visitor_id=vis)])
        for zone in ["SKINCARE", "LIPSTICK", "HAIRCARE"]:
            for _ in range(3 if zone == "SKINCARE" else 1):
                events.append(make_event_helper(event_type="ZONE_ENTER", visitor_id=vis, zone_id=zone))
        ingest_helper(client, events)

        r = client.get("/stores/ST1008/heatmap")
        assert r.status_code == 200
        scores = [z["normalised_score"] for z in r.json()["zones"]]
        assert max(scores) == 100.0
        assert all(0 <= s <= 100 for s in scores)

    def test_data_confidence_flag(self, client, make_event_helper, ingest_helper):
        ingest_helper(client, [make_event_helper(event_type="ZONE_ENTER", zone_id="SKINCARE")])
        r = client.get("/stores/ST1008/heatmap")
        assert r.status_code == 200
        for zone in r.json()["zones"]:
            assert zone["data_confidence"] == False

# PROMPT: Add tests for anomaly detection covering queue spikes, severity levels, empty-store behavior, suggested actions, and conversion drop scenarios.
# CHANGES MADE: Reused shared fixtures from conftest.py and added explicit validation of anomaly types and suggested actions.
import pytest

class TestAnomalies:
    def test_no_anomalies_on_empty_store(self, client):
        r = client.get("/stores/ST1008/anomalies")
        assert r.status_code == 200
        assert r.json()["anomalies"] == []

    def test_queue_spike_warn(self, client, make_event_helper, ingest_helper):
        ingest_helper(client, [
            make_event_helper(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=7)
        ])
        r = client.get("/stores/ST1008/anomalies")
        assert r.status_code == 200
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" in types

    def test_queue_spike_critical(self, client, make_event_helper, ingest_helper):
        ingest_helper(client, [
            make_event_helper(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=12)
        ])
        r = client.get("/stores/ST1008/anomalies")
        crits = [a for a in r.json()["anomalies"] if a["severity"] == "CRITICAL"]
        assert len(crits) >= 1

    def test_anomaly_has_suggested_action(self, client, make_event_helper, ingest_helper):
        ingest_helper(client, [
            make_event_helper(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=8)
        ])
        r = client.get("/stores/ST1008/anomalies")
        for anomaly in r.json()["anomalies"]:
            assert anomaly.get("suggested_action"), "Every anomaly must have a suggested_action"

    def test_conversion_drop_anomaly_fallback(self, client, make_event_helper, ingest_helper):
        # Ingest 20 visitor sessions with billing entries but no POS transactions matching them.
        # This will result in 0% conversion rate, dropping below the 15% fallback baseline.
        events = []
        for i in range(20):
            visitor_id = f"VIS_test_{i}"
            events.extend([
                make_event_helper(event_type="ENTRY", visitor_id=visitor_id),
                make_event_helper(event_type="ZONE_ENTER", visitor_id=visitor_id, zone_id="BILLING")
            ])
        ingest_helper(client, events)

        r = client.get("/stores/ST1008/anomalies")
        assert r.status_code == 200
        anomalies = r.json()["anomalies"]
        types = [a["anomaly_type"] for a in anomalies]
        assert "CONVERSION_DROP" in types

    def test_dead_zone_anomaly_emitted(self, client, make_event_helper, ingest_helper):
        """A zone with activity earlier today but no visit in the last 30+ minutes triggers INFO dead zone."""
        from datetime import datetime, timezone, timedelta
        import uuid
        old_ts = datetime.now(timezone.utc) - timedelta(minutes=45)
        now_ts = datetime.now(timezone.utc)
        vis = f"VIS_{uuid.uuid4().hex[:6]}"
        ingest_helper(client, [
            make_event_helper(event_type="ENTRY", visitor_id=vis, is_staff=False, timestamp=old_ts),
            make_event_helper(event_type="ZONE_ENTER", visitor_id=vis, zone_id="SKINCARE",
                              is_staff=False, timestamp=old_ts),
            make_event_helper(event_type="ENTRY", visitor_id=vis, is_staff=False, timestamp=now_ts),
        ])
        r = client.get("/stores/ST1008/anomalies")
        assert r.status_code == 200
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "DEAD_ZONE" in types
        dead = next(a for a in r.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE")
        assert dead["severity"] == "INFO"
        assert dead["zone_id"] == "SKINCARE"

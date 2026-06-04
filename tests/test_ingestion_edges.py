import pytest
from app.ingestion import ingest_events
from app.models import IngestRequest

# PROMPT: Write unit tests to cover error handling and edge cases in the ingestion endpoint, including non-dict events and invalid date formats.
# CHANGES MADE: Added tests to simulate payload schema failures and tracking alias edge cases.

def test_ingest_events_non_dict_and_invalid_date(db_session):
    class FakeRequest:
        events = [
            "not a dict",
            {
                "event_id": "test_invalid",
                "store_id": "ST1008",
                "camera_id": "CAM1",
                "event_type": "ENTRY",
                "visitor_id": "VIS1",
                "track_id": 999,
                "confidence": 0.9,
                "metadata": {},
                "gender": "M",
                "age": 30,
                "timestamp": "invalid-date-format"
            }
        ]
        
    response = ingest_events(FakeRequest(), db_session)
    assert response.rejected > 0
    assert response.accepted == 0

def test_ingest_events_track_aliases_match(db_session):
    request = IngestRequest(events=[
        {
            "event_id": "test1",
            "store_id": "ST1008",
            "camera_id": "CAM1",
            "event_type": "ENTRY",
            "timestamp": "2026-03-03T10:00:00Z",
            "visitor_id": "VIS1",
            "track_id": 100,
            "confidence": 0.9,
            "metadata": {}
        },
        {
            "event_id": "test2",
            "store_id": "ST1008",
            "camera_id": "CAM1",
            "event_type": "ZONE_DWELL",
            "timestamp": "2026-03-03T10:00:05Z",
            "track_id": 100,
            "confidence": 0.9,
            "zone_id": "ZONE_A",
            "metadata": {}
        }
    ])
    response = ingest_events(request, db_session)
    assert response.accepted == 2

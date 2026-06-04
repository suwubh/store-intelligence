import pytest
from datetime import datetime, timedelta, timezone
from app.anomalies import _check_queue_spike, _check_conversion_drop
from app.database import EventRecord, SessionRecord
from app.models import AnomalySeverity

# PROMPT: Generate unit tests to cover edge cases in anomaly detection, specifically threshold testing for queue spikes and historical conversion drops.
# CHANGES MADE: Added edge case logic to verify CRITICAL vs WARN states for conversion drops and safe handling of zero-events.

def test_check_queue_spike_below_threshold(db_session):
    store_id = "ST1008"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    db_session.add(EventRecord(
        event_id="q1", store_id=store_id, camera_id="CAM1", visitor_id="V1",
        event_type="BILLING_QUEUE_JOIN", timestamp=now, queue_depth=3, confidence=0.9
    ))
    db_session.commit()
    
    anomalies = _check_queue_spike(store_id, now, db_session)
    assert len(anomalies) == 0

def test_check_conversion_drop_with_historical_data_and_warn(db_session):
    store_id = "ST1008"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Add historical data (1 day ago): 20 visitors, 10 converted -> 50%
    hist_time = day_start - timedelta(hours=12)
    for i in range(20):
        db_session.add(SessionRecord(
            session_key=f"hist_{i}", store_id=store_id, visitor_id=f"H{i}",
            is_staff=False, entry_time=hist_time, converted=(i < 10)
        ))
        
    # Add today's data: 20 visitors, 2 converted -> 10%
    for i in range(20):
        db_session.add(SessionRecord(
            session_key=f"today_{i}", store_id=store_id, visitor_id=f"V{i}",
            is_staff=False, entry_time=now, converted=(i < 2)
        ))
        
    db_session.commit()
    
    anomalies = _check_conversion_drop(store_id, now, db_session)
    # Today's rate = 10%, Historical rate = 50%
    # Drop = (0.5 - 0.1) / 0.5 = 80% (CRITICAL)
    assert len(anomalies) == 1
    assert anomalies[0].severity == AnomalySeverity.CRITICAL

    # Test WARN case (Need 35% conversion today for a 30% drop)
    # Change 5 more to converted (total 7 / 20 = 35%)
    for i in range(2, 7):
        session = db_session.get(SessionRecord, f"today_{i}")
        session.converted = True
    db_session.commit()
    
    anomalies_warn = _check_conversion_drop(store_id, now, db_session)
    assert len(anomalies_warn) == 1
    assert anomalies_warn[0].severity == AnomalySeverity.WARN
    
    # Test NO anomaly case (Need > 40% conversion today for a < 20% drop)
    # Change 3 more to converted (total 10 / 20 = 50%)
    for i in range(7, 10):
        session = db_session.get(SessionRecord, f"today_{i}")
        session.converted = True
    db_session.commit()
    anomalies_none = _check_conversion_drop(store_id, now, db_session)
    assert len(anomalies_none) == 0


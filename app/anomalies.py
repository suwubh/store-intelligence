from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_

from app.database import EventRecord, SessionRecord
from app.models import (
    AnomaliesResponse, Anomaly, AnomalySeverity, AnomalyType
)

# Thresholds
QUEUE_SPIKE_THRESHOLD = 5          # depth > 5 → WARN; > 10 → CRITICAL
CONVERSION_DROP_WARN = 0.20        # 20% relative drop vs 7-day avg
CONVERSION_DROP_CRITICAL = 0.40    # 40% relative drop
DEAD_ZONE_MINUTES = 30             # no visits in 30 min → anomaly
STALE_FEED_MINUTES = 10


def get_anomalies(store_id: str, db: Session) -> AnomaliesResponse:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    anomalies: list[Anomaly] = []

    anomalies.extend(_check_queue_spike(store_id, now, db))
    anomalies.extend(_check_conversion_drop(store_id, now, db))
    anomalies.extend(_check_dead_zones(store_id, now, db))

    return AnomaliesResponse(store_id=store_id, anomalies=anomalies)


def _check_queue_spike(store_id: str, now: datetime, db: Session) -> list[Anomaly]:
    recent = now - timedelta(minutes=5)
    result = db.execute(
        select(func.max(EventRecord.queue_depth)).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.event_type == "BILLING_QUEUE_JOIN",
                EventRecord.timestamp >= recent,
                EventRecord.queue_depth.isnot(None),
            )
        )
    ).scalar()

    if result is None:
        return []

    if result > 10:
        severity = AnomalySeverity.CRITICAL
        action = "Deploy additional cashier immediately. Escalate to floor manager."
    elif result > QUEUE_SPIKE_THRESHOLD:
        severity = AnomalySeverity.WARN
        action = "Open secondary billing counter. Monitor queue depth for next 10 minutes."
    else:
        return []

    return [Anomaly(
        anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
        severity=severity,
        zone_id="BILLING",
        description=f"Billing queue depth is {result}. Threshold is {QUEUE_SPIKE_THRESHOLD}.",
        suggested_action=action,
        detected_at=now,
    )]


def _check_conversion_drop(store_id: str, now: datetime, db: Session) -> list[Anomaly]:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _conversion_for_window(start: datetime, end: datetime) -> float:
        total = db.execute(
            select(func.count()).where(
                and_(
                    SessionRecord.store_id == store_id,
                    SessionRecord.is_staff == False,
                    SessionRecord.entry_time >= start,
                    SessionRecord.entry_time < end,
                )
            )
        ).scalar() or 0
        if total == 0:
            return 0.0
        converted = db.execute(
            select(func.count()).where(
                and_(
                    SessionRecord.store_id == store_id,
                    SessionRecord.is_staff == False,
                    SessionRecord.entry_time >= start,
                    SessionRecord.entry_time < end,
                    SessionRecord.converted == True,
                )
            )
        ).scalar() or 0
        return converted / total

    today_rate = _conversion_for_window(day_start, now)

    # 7-day average
    rates = []
    for d in range(1, 8):
        w_start = day_start - timedelta(days=d)
        w_end = w_start + timedelta(days=1)
        r = _conversion_for_window(w_start, w_end)
        if r > 0:
            rates.append(r)

    if not rates:
        return []

    avg_7d = sum(rates) / len(rates)
    if avg_7d == 0:
        return []

    drop = (avg_7d - today_rate) / avg_7d

    if drop >= CONVERSION_DROP_CRITICAL:
        severity = AnomalySeverity.CRITICAL
        action = "Urgent: Review floor staff availability, promotions, and billing counter status."
    elif drop >= CONVERSION_DROP_WARN:
        severity = AnomalySeverity.WARN
        action = "Investigate product placement and staff engagement. Compare with peer stores."
    else:
        return []

    return [Anomaly(
        anomaly_type=AnomalyType.CONVERSION_DROP,
        severity=severity,
        description=f"Today's conversion {today_rate:.1%} is {drop:.1%} below 7-day avg {avg_7d:.1%}.",
        suggested_action=action,
        detected_at=now,
    )]


def _check_dead_zones(store_id: str, now: datetime, db: Session) -> list[Anomaly]:
    cutoff = now - timedelta(minutes=DEAD_ZONE_MINUTES)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Zones that had activity today
    active_zones_today = db.execute(
        select(EventRecord.zone_id).distinct().where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.zone_id.isnot(None),
                EventRecord.timestamp >= day_start,
                EventRecord.is_staff == False,
            )
        )
    ).scalars().all()

    if not active_zones_today:
        return []

    anomalies = []
    for zone_id in active_zones_today:
        last_visit = db.execute(
            select(func.max(EventRecord.timestamp)).where(
                and_(
                    EventRecord.store_id == store_id,
                    EventRecord.zone_id == zone_id,
                    EventRecord.is_staff == False,
                )
            )
        ).scalar()

        if last_visit and last_visit < cutoff:
            idle_minutes = int((now - last_visit).total_seconds() / 60)
            anomalies.append(Anomaly(
                anomaly_type=AnomalyType.DEAD_ZONE,
                severity=AnomalySeverity.INFO,
                zone_id=zone_id,
                description=f"Zone {zone_id} has had no customer visits for {idle_minutes} minutes.",
                suggested_action=f"Check if zone {zone_id} is accessible. Consider moving a staff member to engage customers.",
                detected_at=now,
            ))

    return anomalies

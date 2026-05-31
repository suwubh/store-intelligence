from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_, distinct

from app.database import EventRecord, SessionRecord, POSTransaction
from app.models import StoreMetrics, ZoneDwellMetric


def _day_window(db: Session, store_id: str):
    """
    Return (day_start, day_end) anchored to the earliest event for this store.
    Falls back to UTC today if no events exist yet.
    """
    earliest = db.execute(
        select(func.min(EventRecord.timestamp)).where(EventRecord.store_id == store_id)
    ).scalar()

    if earliest:
        day_start = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
    else:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

    return day_start, day_end


def get_store_metrics(store_id: str, db: Session) -> StoreMetrics:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day_start, day_end = _day_window(db, store_id)

    # Count unique customer visitor_ids that appear in events during the day window.
    # This works even when entry_time is NULL (floor cameras don't set entry_time).
    unique_visitors = db.execute(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
            )
        )
    ).scalar() or 0

    # Total unique sessions (same logic — count by distinct visitor in event window)
    total_sessions = unique_visitors  # one session per unique visitor per day

    # FIX (Issue 5): Converted sessions must be scoped to today's visitors AND
    # to the day window. Previously the query had no date filter, so on a multi-day
    # evaluation, prior days' conversions inflated today's conversion_rate.
    # We scope by joining to today's visitor_ids (already day-filtered above).
    today_visitor_ids = db.execute(
        select(distinct(EventRecord.visitor_id)).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
            )
        )
    ).scalars().all()

    converted_sessions = 0
    if today_visitor_ids:
        converted_sessions = db.execute(
            select(func.count()).where(
                and_(
                    SessionRecord.store_id == store_id,
                    SessionRecord.is_staff == False,
                    SessionRecord.converted == True,
                    SessionRecord.visitor_id.in_(today_visitor_ids),
                )
            )
        ).scalar() or 0

    conversion_rate = (converted_sessions / total_sessions) if total_sessions > 0 else 0.0

    # Avg dwell per zone — uses only ZONE_DWELL events (not ZONE_ENTER which carry dwell_ms=0)
    zone_dwell_rows = db.execute(
        select(
            EventRecord.zone_id,
            func.avg(EventRecord.dwell_ms).label("avg_dwell"),
            func.count(EventRecord.event_id).label("visits"),
        ).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.event_type == "ZONE_DWELL",
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
                EventRecord.zone_id.isnot(None),
            )
        ).group_by(EventRecord.zone_id)
    ).all()

    zone_metrics = [
        ZoneDwellMetric(
            zone_id=row.zone_id,
            avg_dwell_ms=round(row.avg_dwell or 0, 2),
            visit_count=row.visits,
        )
        for row in zone_dwell_rows
    ]

    # Current queue depth (last BILLING_QUEUE_JOIN in past 5 min)
    recent_window = now - timedelta(minutes=5)
    latest_queue = db.execute(
        select(EventRecord.queue_depth).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.event_type == "BILLING_QUEUE_JOIN",
                EventRecord.timestamp >= recent_window,
                EventRecord.queue_depth.isnot(None),
            )
        ).order_by(EventRecord.timestamp.desc()).limit(1)
    ).scalar()
    current_queue_depth = latest_queue or 0

    # Abandonment rate
    billing_visitors = db.execute(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
                EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
                EventRecord.zone_id.in_(["BILLING_COUNTER", "BILLING_QUEUE", "BILLING"]),
            )
        )
    ).scalar() or 0

    abandon_count = db.execute(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.event_type == "BILLING_QUEUE_ABANDON",
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
            )
        )
    ).scalar() or 0

    abandonment_rate = (abandon_count / billing_visitors) if billing_visitors > 0 else 0.0

    return StoreMetrics(
        store_id=store_id,
        window_start=day_start,
        window_end=min(now, day_end),
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_per_zone=zone_metrics,
        current_queue_depth=current_queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
    )
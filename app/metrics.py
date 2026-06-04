from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_, or_, distinct

from app.database import EventRecord, SessionRecord, get_day_window
from app.models import StoreMetrics, ZoneDwellMetric
from app.store_ids import normalize_store_id


def _billing_clause():
    return or_(
        EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"]),
        EventRecord.zone_id.in_(["BILLING_COUNTER", "BILLING_QUEUE", "BILLING"]),
        EventRecord.zone_id.ilike("%BILLING%"),
        EventRecord.sku_zone.ilike("%BILLING%"),
    )


def get_store_metrics(id: str, db: Session) -> StoreMetrics:
    store_id = normalize_store_id(id) or id
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day_start, day_end = get_day_window(db, store_id)

    session_filter = and_(
        SessionRecord.store_id == store_id,
        SessionRecord.is_staff == False,
        SessionRecord.entry_time.isnot(None),
        SessionRecord.entry_time >= day_start,
        SessionRecord.entry_time < day_end,
    )

    unique_visitors = db.execute(
        select(func.count(distinct(SessionRecord.visitor_id))).where(session_filter)
    ).scalar() or 0

    converted_visitors = db.execute(
        select(func.count(distinct(SessionRecord.visitor_id))).where(
            and_(session_filter, SessionRecord.converted == True)
        )
    ).scalar() or 0

    conversion_rate = (converted_visitors / unique_visitors) if unique_visitors > 0 else 0.0

    zone_dwell_rows = db.execute(
        select(
            EventRecord.zone_id,
            func.avg(EventRecord.dwell_ms).label("avg_dwell"),
            func.count(EventRecord.event_id).label("visits"),
        ).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.event_type.in_(
                    ["ZONE_DWELL", "ZONE_EXIT", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"]
                ),
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
                EventRecord.zone_id.isnot(None),
                EventRecord.dwell_ms > 0,
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

    billing_visitors = db.execute(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
                _billing_clause(),
            )
        )
    ).scalar() or 0

    explicit_abandon_count = db.execute(
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

    session_abandon_filter = and_(
        SessionRecord.store_id == store_id,
        SessionRecord.is_staff == False,
        or_(
            and_(SessionRecord.entry_time.isnot(None), SessionRecord.entry_time >= day_start, SessionRecord.entry_time < day_end),
            and_(SessionRecord.billing_at.isnot(None), SessionRecord.billing_at >= day_start, SessionRecord.billing_at < day_end),
        ),
    )
    session_abandon_count = db.execute(
        select(func.count(distinct(SessionRecord.visitor_id))).where(
            and_(
                session_abandon_filter,
                SessionRecord.visited_billing == True,
                SessionRecord.converted == False,
            )
        )
    ).scalar() or 0

    abandon_count = max(explicit_abandon_count, session_abandon_count)
    abandonment_rate = min(1.0, abandon_count / billing_visitors) if billing_visitors > 0 else 0.0

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

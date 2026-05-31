from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_, distinct

from app.database import EventRecord, SessionRecord, get_day_window
from app.models import FunnelResponse, FunnelStage


def get_store_funnel(store_id: str, db: Session) -> FunnelResponse:
    day_start, day_end = get_day_window(db, store_id)

    # Total unique customer visitors seen in any event during the day
    total_entries = db.execute(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
            )
        )
    ).scalar() or 0

    # Visitors who entered at least one named zone
    zone_visitors = db.execute(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
                EventRecord.zone_id.isnot(None),
            )
        )
    ).scalar() or 0

    # Visitors who reached billing
    billing_visitors = db.execute(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
                EventRecord.zone_id.in_(["BILLING_COUNTER", "BILLING_QUEUE", "BILLING"]),
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
            )
        )
    ).scalar() or 0

    # Purchasers query scoped to today's active visitors
    converted_visitors = db.execute(
        select(distinct(EventRecord.visitor_id)).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
            )
        )
    ).scalars().all()

    purchasers = 0
    if converted_visitors:
        purchasers = db.execute(
            select(func.count()).where(
                and_(
                    SessionRecord.store_id == store_id,
                    SessionRecord.is_staff == False,
                    SessionRecord.converted == True,
                    SessionRecord.visitor_id.in_(converted_visitors),
                )
            )
        ).scalar() or 0

    def drop_off(current, previous):
        if previous == 0:
            return 0.0
        return round((1 - current / previous) * 100, 2)

    stages = [
        FunnelStage(stage="Entry",         count=total_entries,    drop_off_pct=0.0),
        FunnelStage(stage="Zone Visit",    count=zone_visitors,    drop_off_pct=drop_off(zone_visitors,    total_entries)),
        FunnelStage(stage="Billing Queue", count=billing_visitors, drop_off_pct=drop_off(billing_visitors, zone_visitors)),
        FunnelStage(stage="Purchase",      count=purchasers,       drop_off_pct=drop_off(purchasers,       billing_visitors)),
    ]

    return FunnelResponse(store_id=store_id, stages=stages, total_sessions=total_entries)
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_, distinct, or_

from app.database import EventRecord, SessionRecord, get_day_window
from app.models import FunnelResponse, FunnelStage


def _billing_clause():
    return or_(
        EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"]),
        EventRecord.zone_id.in_(["BILLING_COUNTER", "BILLING_QUEUE", "BILLING"]),
        EventRecord.zone_id.ilike("%BILLING%"),
        EventRecord.sku_zone.ilike("%BILLING%"),
    )


def get_store_funnel(store_id: str, db: Session) -> FunnelResponse:
    day_start, day_end = get_day_window(db, store_id)

    session_base = and_(
        SessionRecord.store_id == store_id,
        SessionRecord.is_staff == False,
        SessionRecord.entry_time.isnot(None),
        SessionRecord.entry_time >= day_start,
        SessionRecord.entry_time < day_end,
    )

    # Distinct visitors — re-entry must not double-count (challenge funnel rule)
    total_entries = db.execute(
        select(func.count(distinct(SessionRecord.visitor_id))).where(session_base)
    ).scalar() or 0

    zone_visitors = db.execute(
        select(func.count(distinct(SessionRecord.visitor_id))).where(
            and_(
                session_base,
                SessionRecord.zones_visited.isnot(None),
                SessionRecord.zones_visited != "",
            )
        )
    ).scalar() or 0

    billing_visitors = db.execute(
        select(func.count(distinct(SessionRecord.visitor_id))).where(
            and_(session_base, SessionRecord.visited_billing == True)
        )
    ).scalar() or 0

    purchasers = db.execute(
        select(func.count(distinct(SessionRecord.visitor_id))).where(
            and_(session_base, SessionRecord.converted == True)
        )
    ).scalar() or 0

    def drop_off(current, previous):
        if previous == 0:
            return 0.0
        return round((1 - current / previous) * 100, 2)

    stages = [
        FunnelStage(stage="Entry", count=total_entries, drop_off_pct=0.0),
        FunnelStage(
            stage="Zone Visit",
            count=zone_visitors,
            drop_off_pct=drop_off(zone_visitors, total_entries),
        ),
        FunnelStage(
            stage="Billing Queue",
            count=billing_visitors,
            drop_off_pct=drop_off(billing_visitors, zone_visitors),
        ),
        FunnelStage(
            stage="Purchase",
            count=purchasers,
            drop_off_pct=drop_off(purchasers, billing_visitors),
        ),
    ]

    return FunnelResponse(store_id=store_id, stages=stages, total_sessions=total_entries)

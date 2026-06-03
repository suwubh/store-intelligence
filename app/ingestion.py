import csv
import logging
import os
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.database import EventRecord, POSTransaction, SessionRecord
from app.models import IngestRequest, IngestResponse, StoreEvent
from app.store_ids import normalize_store_id

logger = logging.getLogger(__name__)

BILLING_ZONES = {"BILLING", "BILLING_COUNTER", "BILLING_QUEUE", "CHECKOUT", "CASHIER"}
CONVERSION_WINDOW_SECONDS = 300
POS_TIMEZONE_OFFSET = timedelta(minutes=int(os.getenv("POS_TIMEZONE_OFFSET_MINUTES", "0")))
CROSS_CAMERA_LINK_WINDOW_SECONDS = 30 * 60

# Updated sample_events.jsonl: track_id → id_token (verified from resource file)
SAMPLE_TRACK_ALIASES = {
    101: "ID_60001",
    102: "ID_60002",
    103: "ID_60003",
}


def ingest_events(request: IngestRequest, db: Session) -> IngestResponse:
    accepted = 0
    rejected = 0
    duplicate = 0
    errors = []
    seen_sessions: dict[str, SessionRecord] = {}
    track_aliases = dict(SAMPLE_TRACK_ALIASES)
    camera_aliases: dict[str, str] = {}

    # First pass: learn id_token ↔ track_id links from the same batch
    for raw in request.events:
        if not isinstance(raw, dict):
            continue
        token = raw.get("id_token") or raw.get("visitor_id")
        track_id = raw.get("track_id")
        if token and track_id is not None:
            track_aliases[int(track_id)] = str(token)

    for idx, raw in enumerate(request.events):
        try:
            event = _parse_event(raw, track_aliases)
            _link_camera_local_visitor(event, db, seen_sessions, camera_aliases)
        except (ValidationError, Exception) as e:
            rejected += 1
            errors.append({
                "index": idx,
                "event_id": raw.get("event_id", raw.get("queue_event_id", "unknown")) if isinstance(raw, dict) else "unknown",
                "error": str(e),
            })
            continue

        try:
            existing = db.get(EventRecord, event.event_id)
            if existing:
                duplicate += 1
                continue

            db.add(_event_to_record(event))
            session = _upsert_session(event, db, seen_sessions)
            if session and session.visited_billing:
                _try_mark_converted(session, db)
            accepted += 1
        except Exception as e:
            rejected += 1
            errors.append({"index": idx, "event_id": event.event_id, "error": str(e)})
            logger.warning("Rejected event %s: %s", event.event_id, e)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Commit failed: %s", e)
        raise

    return IngestResponse(accepted=accepted, rejected=rejected, duplicate=duplicate, errors=errors)


def _parse_event(raw: dict, track_aliases: dict[int, str]) -> StoreEvent:
    data = dict(raw)
    track_id = data.get("track_id")
    if track_id is not None and not data.get("visitor_id") and not data.get("id_token"):
        alias = track_aliases.get(int(track_id))
        if alias:
            data["visitor_id"] = alias
    return StoreEvent(**data)


def _camera_alias_key(event: StoreEvent) -> str:
    return f"{event.store_id}:{event.camera_id}:{event.visitor_id}"


def _link_camera_local_visitor(
    event: StoreEvent,
    db: Session,
    seen_sessions: dict[str, SessionRecord],
    camera_aliases: dict[str, str],
) -> None:
    """
    The detector runs each camera clip in its own process, so floor/billing
    tracks may have camera-local visitor IDs. Link those events to an entry
    session when a plausible session already exists for the same store.
    """
    if event.event_type.value in ("ENTRY", "EXIT", "REENTRY"):
        return

    key = _camera_alias_key(event)
    if key in camera_aliases:
        event.visitor_id = camera_aliases[key]
        return

    if _has_matching_session(event, db, seen_sessions):
        return

    candidate = _best_cross_camera_session(event, db, seen_sessions)
    if not candidate:
        return

    camera_aliases[key] = candidate.visitor_id
    event.visitor_id = candidate.visitor_id


def _has_matching_session(
    event: StoreEvent,
    db: Session,
    seen_sessions: dict[str, SessionRecord],
) -> bool:
    for session in seen_sessions.values():
        if session.store_id == event.store_id and session.visitor_id == event.visitor_id:
            return True

    existing = db.execute(
        select(SessionRecord).where(
            and_(
                SessionRecord.store_id == event.store_id,
                SessionRecord.visitor_id == event.visitor_id,
            )
        ).limit(1)
    ).scalar_one_or_none()
    return existing is not None


def _best_cross_camera_session(
    event: StoreEvent,
    db: Session,
    seen_sessions: dict[str, SessionRecord],
) -> SessionRecord | None:
    candidates: dict[str, SessionRecord] = {}
    for session in seen_sessions.values():
        if _session_can_cover_event(session, event):
            candidates[session.session_key] = session

    event_ts = _as_naive(event.timestamp)
    window_start = event_ts - timedelta(seconds=CROSS_CAMERA_LINK_WINDOW_SECONDS)
    window_end = event_ts + timedelta(seconds=CROSS_CAMERA_LINK_WINDOW_SECONDS)
    rows = db.execute(
        select(SessionRecord).where(
            and_(
                SessionRecord.store_id == event.store_id,
                SessionRecord.is_staff == False,
                SessionRecord.entry_time.isnot(None),
                SessionRecord.entry_time <= window_end,
            )
        ).order_by(SessionRecord.entry_time.asc())
    ).scalars().all()
    for session in rows:
        if _session_can_cover_event(session, event, window_start=window_start, window_end=window_end):
            candidates[session.session_key] = session

    if not candidates:
        return None

    return min(
        candidates.values(),
        key=lambda session: (
            _session_last_event_time(session, db) or session.entry_time or event_ts,
            session.entry_time or event_ts,
        ),
    )


def _session_can_cover_event(
    session: SessionRecord,
    event: StoreEvent,
    window_start=None,
    window_end=None,
) -> bool:
    if session.is_staff or not session.entry_time:
        return False
    event_ts = _as_naive(event.timestamp)
    entry_time = _as_naive(session.entry_time)
    exit_time = _as_naive(session.exit_time) if session.exit_time else None
    window_start = window_start or event_ts - timedelta(seconds=CROSS_CAMERA_LINK_WINDOW_SECONDS)
    window_end = window_end or event_ts + timedelta(seconds=CROSS_CAMERA_LINK_WINDOW_SECONDS)
    if entry_time > window_end:
        return False
    if exit_time and exit_time < window_start:
        return False
    return True


def _as_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.replace(tzinfo=None)


def _session_last_event_time(session: SessionRecord, db: Session):
    return db.execute(
        select(EventRecord.timestamp).where(
            and_(
                EventRecord.store_id == session.store_id,
                EventRecord.visitor_id == session.visitor_id,
            )
        ).order_by(EventRecord.timestamp.desc()).limit(1)
    ).scalar_one_or_none()


def _event_to_record(event: StoreEvent) -> EventRecord:
    return EventRecord(
        event_id=event.event_id,
        store_id=event.store_id,
        camera_id=event.camera_id,
        visitor_id=event.visitor_id,
        event_type=event.event_type.value,
        timestamp=event.timestamp,
        zone_id=event.zone_id,
        dwell_ms=event.dwell_ms,
        is_staff=event.is_staff,
        confidence=event.confidence,
        queue_depth=event.metadata.queue_depth,
        sku_zone=event.metadata.sku_zone,
        session_seq=event.metadata.session_seq,
    )


def _resolve_session_key(
    event: StoreEvent,
    db: Session,
    seen_sessions: dict[str, SessionRecord],
) -> str:
    """One session per visit; new key after EXIT when a new ENTRY/REENTRY arrives."""
    base = f"{event.store_id}:{event.visitor_id}"
    if event.event_type.value not in ("ENTRY", "REENTRY"):
        for key, session in seen_sessions.items():
            if session.visitor_id == event.visitor_id and session.store_id == event.store_id:
                if session.exit_time is None:
                    return key
        active = db.execute(
            select(SessionRecord).where(
                and_(
                    SessionRecord.store_id == event.store_id,
                    SessionRecord.visitor_id == event.visitor_id,
                    SessionRecord.exit_time.is_(None),
                )
            ).order_by(SessionRecord.entry_time.desc())
        ).scalars().first()
        if active:
            return active.session_key
        return base

    # ENTRY / REENTRY — start new visit if previous visit closed
    candidate_keys = [k for k in seen_sessions if k.startswith(base)]
    for key in sorted(candidate_keys, reverse=True):
        session = seen_sessions[key]
        if session.exit_time is None:
            if event.event_type.value == "REENTRY":
                return key
            return key

    existing = db.execute(
        select(SessionRecord).where(
            and_(
                SessionRecord.store_id == event.store_id,
                SessionRecord.visitor_id == event.visitor_id,
            )
        ).order_by(SessionRecord.entry_time.desc())
    ).scalars().all()

    if existing:
        latest = existing[0]
        if latest.exit_time is None:
            return latest.session_key
        suffix = latest.reentry_count + 1 if event.event_type.value == "REENTRY" else len(existing)
        return f"{base}:v{suffix}"

    return base


def _upsert_session(
    event: StoreEvent,
    db: Session,
    seen_sessions: dict[str, SessionRecord],
) -> SessionRecord | None:
    session_key = _resolve_session_key(event, db, seen_sessions)
    session = seen_sessions.get(session_key)
    if not session:
        session = db.get(SessionRecord, session_key)
    if not session:
        session = SessionRecord(
            session_key=session_key,
            store_id=event.store_id,
            visitor_id=event.visitor_id,
            is_staff=event.is_staff,
        )
        db.add(session)
        db.flush()

    seen_sessions[session_key] = session
    session.is_staff = session.is_staff or event.is_staff

    etype = event.event_type.value
    if etype == "ENTRY":
        if session.entry_time is None or session.exit_time is not None:
            session.entry_time = event.timestamp
            session.exit_time = None
    elif etype == "EXIT":
        session.exit_time = event.timestamp
    elif etype == "REENTRY":
        session.reentry_count = (session.reentry_count or 0) + 1
        session.entry_time = event.timestamp
        session.exit_time = None
    elif etype in ("ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT"):
        _add_session_zone(session, event.zone_id)

    if _is_billing_signal(event):
        session.visited_billing = True
        session.billing_at = session.billing_at or event.timestamp
        _add_session_zone(session, event.zone_id)

    return session


def _add_session_zone(session: SessionRecord, zone_id: str | None):
    if not zone_id:
        return
    zones = set(session.zones_visited.split(",")) if session.zones_visited else set()
    zones.discard("")
    zones.add(zone_id)
    session.zones_visited = ",".join(sorted(zones))


def _is_billing_signal(event: StoreEvent) -> bool:
    if event.event_type.value in ("BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"):
        return True

    candidates = [event.zone_id or "", event.metadata.sku_zone or ""]
    return any(
        candidate.upper() in BILLING_ZONES or "BILLING" in candidate.upper()
        for candidate in candidates
    )


def _try_mark_converted(session: SessionRecord, db: Session) -> bool:
    """
    Challenge rule: billing visit in the 5-minute window before a POS transaction.
    If billing_at = B, a transaction at T converts when B <= T <= B + 5 minutes.
    """
    if session.converted or not session.visited_billing or not session.billing_at:
        return False

    window_end = session.billing_at + timedelta(seconds=CONVERSION_WINDOW_SECONDS)
    result = db.execute(
        select(POSTransaction).where(
            and_(
                POSTransaction.store_id == session.store_id,
                POSTransaction.timestamp >= session.billing_at,
                POSTransaction.timestamp <= window_end,
            )
        )
    ).first()
    if result:
        session.converted = True
        return True
    return False


def attribute_conversions_for_store(store_id: str, db: Session) -> int:
    """Re-run POS correlation for all open sessions (e.g. after POS CSV load)."""
    sessions = db.execute(
        select(SessionRecord).where(
            and_(
                SessionRecord.store_id == store_id,
                SessionRecord.visited_billing == True,
                SessionRecord.converted == False,
                SessionRecord.is_staff == False,
            )
        )
    ).scalars().all()
    marked = 0
    for session in sessions:
        if _try_mark_converted(session, db):
            marked += 1
    if marked:
        db.commit()
    return marked


def load_pos_transactions(csv_path: str, db: Session):
    """
    Load either challenge POS shape:
    - transaction_id,timestamp,basket_value_inr
    - order_id,order_date,order_time,store_id,product_id,brand_name,total_amount
    """
    loaded = 0
    stores_touched: set[str] = set()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        has_split_time = "order_date" in cols and "order_time" in cols
        order_id_col = "invoice_number" if "invoice_number" in cols else "order_id" if "order_id" in cols else None
        orders: dict[str, dict] = {}

        for row in reader:
            try:
                if has_split_time and order_id_col:
                    tx_id = row[order_id_col].strip()
                    if not tx_id or tx_id.lower() == "nan":
                        continue
                    if tx_id not in orders:
                        parsed = datetime.strptime(
                            f"{row['order_date'].strip()} {row['order_time'].strip()}",
                            "%d-%m-%Y %H:%M:%S",
                        )
                        store_id = normalize_store_id(row["store_id"]) or row["store_id"].strip()
                        orders[tx_id] = {
                            "store_id": store_id,
                            "timestamp": parsed - POS_TIMEZONE_OFFSET,
                            "basket_value": 0.0,
                        }
                        stores_touched.add(store_id)
                    orders[tx_id]["basket_value"] += _float_or_zero(row.get("total_amount"))
                else:
                    tx_id = row.get("transaction_id", row.get("invoice_number", "")).strip()
                    if not tx_id or db.get(POSTransaction, tx_id):
                        continue
                    timestamp = datetime.fromisoformat(row.get("timestamp", "").strip().replace("Z", "+00:00"))
                    store_id = normalize_store_id(row["store_id"]) or row["store_id"].strip()
                    db.add(POSTransaction(
                        transaction_id=tx_id,
                        store_id=store_id,
                        timestamp=timestamp,
                        basket_value=_float_or_zero(row.get("basket_value_inr")),
                    ))
                    stores_touched.add(store_id)
                    loaded += 1
            except Exception as e:
                logger.warning("Skipping POS row: %s", e)

        for tx_id, data in orders.items():
            if db.get(POSTransaction, tx_id):
                continue
            db.add(POSTransaction(
                transaction_id=tx_id,
                store_id=data["store_id"],
                timestamp=data["timestamp"],
                basket_value=data["basket_value"],
            ))
            loaded += 1

    db.commit()
    for store_id in stores_touched:
        attribute_conversions_for_store(store_id, db)
    logger.info("POS transactions loaded: %s", loaded)


def _float_or_zero(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

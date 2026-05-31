import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import select, and_
from pydantic import ValidationError

from app.models import StoreEvent, IngestRequest, IngestResponse
from app.database import EventRecord, SessionRecord, POSTransaction

logger = logging.getLogger(__name__)

BILLING_ZONES = {"BILLING", "BILLING_COUNTER", "BILLING_QUEUE", "CHECKOUT", "CASHIER"}
# A visitor at billing UP TO 5 min BEFORE a transaction = converted
CONVERSION_WINDOW_SECONDS = 300

# POS transactions in the CSV use Indian Standard Time (IST, UTC+5:30).
# Subtraction of 5h30m converts IST to UTC-naive datetimes to align with pipeline event timestamps.
IST_TO_UTC_OFFSET = timedelta(hours=5, minutes=30)


def ingest_events(request: IngestRequest, db: Session) -> IngestResponse:
    accepted = 0
    rejected = 0
    duplicate = 0
    errors = []
    seen_sessions: dict = {}  # in-memory cache for this batch — prevents duplicate inserts

    for idx, raw in enumerate(request.events):
        try:
            event = StoreEvent(**raw) if isinstance(raw, dict) else raw
        except (ValidationError, Exception) as e:
            rejected += 1
            errors.append({
                "index": idx,
                "event_id": raw.get("event_id", "unknown") if isinstance(raw, dict) else "unknown",
                "error": str(e),
            })
            continue

        try:
            existing = db.get(EventRecord, event.event_id)
            if existing:
                duplicate += 1
                continue

            record = _event_to_record(event)
            db.add(record)
            _upsert_session(event, db, seen_sessions)
            accepted += 1

        except Exception as e:
            rejected += 1
            errors.append({"index": idx, "event_id": event.event_id, "error": str(e)})
            logger.warning(f"Rejected event {event.event_id}: {e}")

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Commit failed: {e}")
        raise

    return IngestResponse(accepted=accepted, rejected=rejected, duplicate=duplicate, errors=errors)


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


def _upsert_session(event: StoreEvent, db: Session, seen_sessions: dict):
    session_key = f"{event.store_id}:{event.visitor_id}"

    # Check in-memory cache first — prevents duplicate inserts within same batch
    if session_key in seen_sessions:
        session = seen_sessions[session_key]
    else:
        session = db.get(SessionRecord, session_key)
        if not session:
            session = SessionRecord(
                session_key=session_key,
                store_id=event.store_id,
                visitor_id=event.visitor_id,
                is_staff=event.is_staff,
            )
            db.add(session)
            db.flush()  # write to DB so subsequent db.get() calls see it
        seen_sessions[session_key] = session

    etype = event.event_type.value

    if etype == "ENTRY":
        if session.entry_time is None:
            session.entry_time = event.timestamp
    elif etype == "EXIT":
        session.exit_time = event.timestamp
    elif etype == "REENTRY":
        session.reentry_count = (session.reentry_count or 0) + 1
        session.entry_time = event.timestamp  # reset session window for conversion
    elif etype in ("ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT"):
        if event.zone_id:
            zones = set(session.zones_visited.split(",")) if session.zones_visited else set()
            zones.discard("")
            zones.add(event.zone_id)
            session.zones_visited = ",".join(zones)

    # Mark billing visit
    if event.zone_id and event.zone_id.upper() in BILLING_ZONES:
        session.visited_billing = True
    if etype in ("BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"):
        session.visited_billing = True

    # Check conversion: was there a POS txn within 5 min AFTER this visitor
    # was at the billing zone? (visitor bills → transaction fires shortly after)
    if session.visited_billing and not session.converted:
        session.converted = _check_conversion(event.store_id, event.timestamp, db)


def _check_conversion(store_id: str, ref_time: datetime, db: Session) -> bool:
    """
    Return True if a POS transaction exists in the 5-minute window AFTER ref_time.
    The visitor hits billing → transaction happens within 5 min → converted.
    Also check 2 min before (cashier may have started ringing before we detect billing visit).
    """
    window_start = ref_time - timedelta(seconds=120)   # 2 min buffer before
    window_end   = ref_time + timedelta(seconds=CONVERSION_WINDOW_SECONDS)

    result = db.execute(
        select(POSTransaction).where(
            and_(
                POSTransaction.store_id == store_id,
                POSTransaction.timestamp >= window_start,
                POSTransaction.timestamp <= window_end,
            )
        )
    ).first()
    return result is not None


def load_pos_transactions(csv_path: str, db: Session):
    """
    Load POS CSV into DB.
    Handles the real Purplle 39-col format:
      order_date (DD-MM-YYYY), order_time (HH:MM:SS), invoice_number, store_id, total_amount

    # POS timestamps are in IST. Subtracting the 5h30m offset aligns them with UTC-naive
    # pipeline event timestamps for correct conversion window matching.
    """
    import csv

    loaded = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        is_purplle = "invoice_number" in cols and "order_date" in cols
        invoices: dict = {}

        for row in reader:
            try:
                if is_purplle:
                    inv_id = row["invoice_number"].strip()
                    if not inv_id or inv_id == "nan":
                        continue
                    if inv_id not in invoices:
                        # Parse DD-MM-YYYY HH:MM:SS as IST wall clock, then convert to UTC-naive
                        dt_ist = datetime.strptime(
                            f"{row['order_date'].strip()} {row['order_time'].strip()}",
                            "%d-%m-%Y %H:%M:%S",
                        )
                        # FIX (Issue N1): subtract 5h30m to convert IST → UTC
                        dt_utc = dt_ist - IST_TO_UTC_OFFSET
                        invoices[inv_id] = {
                            "store_id": row["store_id"].strip(),
                            "timestamp": dt_utc,
                            "basket_value": 0.0,
                        }
                    try:
                        invoices[inv_id]["basket_value"] += float(row.get("total_amount", 0) or 0)
                    except (ValueError, TypeError):
                        pass
                else:
                    tx_id = row.get("transaction_id", row.get("invoice_number", "")).strip()
                    if not tx_id:
                        continue
                    if db.get(POSTransaction, tx_id):
                        continue
                    dt = datetime.fromisoformat(row.get("timestamp", "").strip().replace("Z", "+00:00"))
                    db.add(POSTransaction(
                        transaction_id=tx_id,
                        store_id=row["store_id"].strip(),
                        timestamp=dt,
                        basket_value=float(row.get("basket_value_inr", 0)),
                    ))
                    loaded += 1
            except Exception as e:
                logger.warning(f"Skipping POS row: {e}")

        if is_purplle:
            for inv_id, data in invoices.items():
                if db.get(POSTransaction, inv_id):
                    continue
                db.add(POSTransaction(
                    transaction_id=inv_id,
                    store_id=data["store_id"],
                    timestamp=data["timestamp"],
                    basket_value=data["basket_value"],
                ))
                loaded += 1

    db.commit()
    logger.info(f"POS transactions loaded: {loaded}")
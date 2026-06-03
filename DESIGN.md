# DESIGN.md - Store Intelligence API

## System Overview

The system turns anonymised retail CCTV into a store analytics API. It has four stages:

1. Detection reads raw clips, detects people, tracks movement, flags staff, and emits structured events.
2. Event ingestion accepts batches, validates them, deduplicates by event identity, and stores canonical event records.
3. Session materialisation updates visitor state during ingest so funnel and conversion queries do not need to rebuild sessions from scratch.
4. Intelligence endpoints expose metrics, funnel, heatmap, anomalies, health, and a live dashboard surface.

The north-star metric is offline store conversion rate: unique non-staff visitors who completed a purchase divided by total unique non-staff visitor sessions.

## Updated Resource Contract

The revised challenge resources changed the data contract more than the product goal. The implementation now treats the uploaded sample files as authoritative:

- The API accepts the original uppercase event schema emitted by `pipeline/emit.py`.
- The API also accepts the updated lower-case sample event schema: `entry`, `exit`, `zone_entered`, `zone_exited`, `queue_completed`, and `queue_abandoned`.
- `store_code` values such as `store_1076` are normalized to `ST1076`.
- Rows without `event_id` get deterministic UUIDv5 IDs from the payload, preserving idempotency on replay.
- The updated POS CSV is parsed as line-item data and aggregated by `order_id`.
- Billing zones are detected by exact old IDs and by semantic IDs containing `BILLING`, such as `PURPLLE_MUM_1076_Z_BILLING_01`.
- Store folder IDs are normalized to the POS/sample contracts: Store 1 -> `ST1008`, Store 2 -> `ST1076`.

This keeps the database and endpoint layer stable while making ingestion tolerant to both schemas.

## Detection Layer

The detection pipeline uses YOLOv8s for person detection and a multi-object tracker for session-level visitor IDs. The detector emits the original challenge schema because that schema is still the clearest contract for the raw-video pipeline: uppercase event types, ISO timestamp, `visitor_id`, `zone_id`, `dwell_ms`, `is_staff`, `confidence`, and `metadata`.

Camera discovery is intentionally defensive. The updated store ZIPs use names like `CAM 1 - zone.mp4`, `entry 1.mp4`, and `billing_area.mp4`, so `pipeline/run.bat` and `pipeline/run.sh` first try `store_layout.json` `source_file` mappings, then infer camera role from filename. This lets the pipeline run on newly extracted store folders before full polygon calibration is complete.

Layout PNGs are accepted by the dataset validator, but polygon zone mapping still requires JSON. If a PNG path is passed to the zone mapper, the mapper disables zone polygons with a warning instead of crashing. That is a deliberate graceful-degradation choice: entry/exit and queue-level processing can still be demonstrated while precise zone polygons are calibrated.

The runner processes entry cameras before floor and billing cameras. Because each camera clip is processed in its own detector process, floor and billing track IDs are camera-local. During ingestion, non-entry events without an exact session match are linked to a plausible non-staff entry session in the same store and time window. This is not the same as a production OSNet/StrongSORT cross-camera model, but it prevents the separate-process pipeline from creating unrelated funnel sessions for the same physical visit.

Staff detection is store-specific. Store 1 uses an all-black uniform profile. Store 2 uses a pink-top/black-bottom HSV profile. These profiles are intentionally exposed in `store_layout.json` so they can be recalibrated without changing endpoint logic.

## API and Storage

FastAPI is the service boundary. SQLite is used through SQLAlchemy because the acceptance gate prioritizes `docker compose up` reliability with no external setup. Events are immutable rows. Sessions are materialized rows keyed by `store_id:visitor_id`, updated during ingest.

The API is idempotent: replaying the same event file returns duplicates instead of inserting twice. Partial success is supported, so malformed rows are reported by index while valid rows are still accepted. This matters for generated detection streams, where a few bad events should not discard a full batch.

Conversion attribution follows the challenge rule: a visitor who reached billing within the five-minute transaction window is counted as converted. The POS loader defaults to timezone-less local timestamps because the updated files do not include timezone offsets. If a run uses UTC event timestamps with IST POS exports, `POS_TIMEZONE_OFFSET_MINUTES=330` aligns them.

## AI-Assisted Decisions

### 1. Contract adapter instead of a rewrite

AI review highlighted that the new sample events did not match the original Pydantic model. The first suggestion was to replace the event table with a new wide schema. I rejected that because it would have forced broad changes across metrics, funnel, heatmap, anomalies, tests, and dashboard. The better design was an adapter at the model boundary: normalize old and new payloads into the existing canonical `StoreEvent`.

### 2. Deterministic IDs for sample events

The updated sample events do not include `event_id` on entry and zone rows. AI suggested generating UUIDv4 values, but that would break idempotency because the same file replayed twice would create different IDs. I changed the design to UUIDv5 over the canonical payload. That makes missing IDs stable while preserving global uniqueness for practical challenge inputs.

### 3. Treat uploaded files as authoritative

The written problem statement still shows the older `transaction_id,timestamp,basket_value_inr` POS shape, while the uploaded CSV uses `order_id,order_date,order_time,total_amount`. AI analysis called out this contradiction. I agreed and made the implementation support both, with the updated files driving tests. This is important because automated evaluation usually follows the resource files more closely than prose examples.

### 4. Graceful layout handling

AI suggested trying to infer polygons from the layout PNG automatically. I did not implement that because it would be fragile without human calibration and could create false confidence in zone metrics. The code instead validates layout images as received assets, keeps detection runnable, and makes polygon JSON the explicit calibration step.

## Known Limitations

Cross-camera Re-ID is still heuristic and not a production-grade appearance embedding model. Staff classification is primarily visual and can fail when customer clothing resembles staff uniform. Layout PNGs are not automatically converted into product-zone polygons. These are scoped trade-offs for the challenge window; the API contract and ingestion layer are designed so better detection events can be replayed without changing endpoint logic.

# DESIGN.md - Store Intelligence API

## System Overview

The Store Intelligence API transforms anonymised retail CCTV into an actionable analytics service. The architecture is divided into four distinct stages:

1. **Detection Pipeline**: Processes raw clips to detect individuals, track movement, identify staff members, and emit structured events.
2. **Event Ingestion**: Receives event batches, applies schema validation, deduplicates by event identity, and commits records to storage.
3. **Session Materialisation**: Updates visitor state dynamically during ingestion, preventing expensive query-time session reconstruction for funnel and conversion metrics.
4. **Intelligence API**: Exposes endpoints for real-time metrics, funnels, heatmaps, anomalies, health monitoring, and a live dashboard surface.

The primary KPI tracked by this system is the offline store conversion rate, calculated as the ratio of unique non-staff visitors who completed a purchase to the total unique non-staff visitor sessions.

## Resource Contract

The API operates under a strict data contract designed to accommodate variations in input formats:

- The ingest layer accepts the original uppercase event schema emitted by `pipeline/emit.py`.
- It concurrently supports the lowercase sample event schema (`entry`, `exit`, `zone_entered`, `zone_exited`, `queue_completed`, `queue_abandoned`).
- Identifiers are automatically normalized (e.g., `store_1076` resolves to `ST1076`).
- Records lacking an `event_id` are assigned deterministic UUIDv5 IDs derived from the payload, ensuring idempotency upon replay.
- The POS CSV dataset is processed as line-item data and aggregated by `order_id`.
- Billing zones are identified through historical ID mapping or semantic matching (e.g., IDs containing `BILLING`).

This approach isolates the database and endpoint layers from upstream data inconsistencies while providing a robust ingestion interface.

## Detection Layer

The detection pipeline utilizes YOLOv8s for person detection combined with a multi-object tracker to maintain session-level visitor IDs. The pipeline outputs the standard challenge schema: uppercase event types, ISO timestamps, `visitor_id`, `zone_id`, `dwell_ms`, `is_staff`, `confidence`, and `metadata`.

Camera discovery relies on a defensive strategy. When analyzing directories with varying nomenclature (e.g., `CAM 1 - zone.mp4`, `entry 1.mp4`), the execution scripts first reference `store_layout.json` for mapping. If unresolved, they infer camera roles directly from the filename. This permits rapid execution on newly extracted store directories prior to comprehensive polygon calibration.

Layout PNGs serve as reference assets during validation. If a PNG path is mistakenly provided to the zone mapper, the system disables zone polygons with a warning instead of failing. This graceful degradation allows entry, exit, and queue-level analytics to function while explicit polygon coordinates are calibrated.

The pipeline processes entry cameras sequentially before floor and billing cameras. To resolve track continuity across independent camera processes, the ingestion layer associates non-entry events lacking a precise session match to a plausible non-staff entry session within the identical store and time window. While distinct from a fully integrated cross-camera Re-ID model, this heuristic effectively aligns standalone camera outputs into unified funnel sessions.

Staff detection implements store-specific HSV profiles. Store 1 relies on a solid black uniform profile, whereas Store 2 uses a pink-top and black-bottom profile. These configurations are exposed within `store_layout.json` to facilitate recalibration without requiring endpoint modifications.

## API and Storage

FastAPI serves as the service boundary, and SQLite manages persistence via SQLAlchemy. SQLite was selected to fulfill the primary deployment requirement of executing via `docker compose up` with zero external dependencies. Events are modeled as immutable records, while sessions function as materialized rows keyed by `store_id:visitor_id` and are updated sequentially during ingest.

The API is fully idempotent. Replaying an identical event file yields deduplication rather than redundant insertion. The system supports partial success for batch payloads, isolating malformed records while committing valid events. This ensures minor detection anomalies do not invalidate entire ingestion batches.

Conversion attribution applies a standardized rule: a visitor present in the billing zone within a five-minute window preceding a POS transaction is marked as converted. POS data is parsed utilizing local timestamps, and offsets can be applied via the `POS_TIMEZONE_OFFSET_MINUTES` configuration when discrepancies exist between POS systems and event logs.

## AI-Assisted Decisions

### 1. Contract Adapter at the Model Boundary

Initial AI review noted a discrepancy between newly supplied sample events and the original Pydantic model. The AI proposed introducing a wide schema table to handle all fields. This was rejected, as it would require sweeping modifications to downstream metric, funnel, and heatmap logic. Instead, a boundary adapter pattern was implemented. Incoming payloads are normalized into the established `StoreEvent` model, maintaining the stability of the core application logic.

### 2. Deterministic IDs for Sample Events

The provided sample events omitted the `event_id` field for specific entry and zone rows. AI analysis suggested generating UUIDv4 values during ingest. This approach was discarded because it would violate idempotency; re-ingesting the same file would yield duplicate records. A UUIDv5 generator based on the canonical payload was implemented instead, achieving stability and global uniqueness for all practical inputs.

### 3. Authoritative Handling of Uploaded Files

A structural contradiction existed between the written specification for POS data (`transaction_id,timestamp,basket_value_inr`) and the provided dataset (`order_id,order_date,order_time,total_amount`). AI evaluation identified this discrepancy. The implementation was designed to support both formats, with the provided dataset acting as the authoritative source for testing.

### 4. Polygon Inference Handling

AI suggestions included building an automated parser to infer zone polygons from the layout PNGs. This feature was excluded because automated inference is inherently fragile and would introduce false confidence in zone analytics without human verification. The architecture instead enforces explicit JSON polygon configuration, while ensuring the detection pipeline remains runnable even when precise definitions are absent.

## Known Limitations

Cross-camera Re-ID relies on heuristic matching rather than a production-grade appearance embedding model. Staff classification depends strictly on visual color profiles and is susceptible to false positives when customer attire mirrors staff uniforms. Additionally, layout PNGs require manual calibration to define product-zone polygons. These engineering trade-offs align with the constraint window; the ingestion layer and API contract guarantee that future model improvements can be integrated directly without modifying downstream endpoint logic.

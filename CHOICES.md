# CHOICES.md — Engineering Decisions

## Decision 1: Detection Model — YOLOv8s

### Options Considered

| Model | Pros | Cons |
|-------|------|------|
| YOLOv8n (nano) | Fastest execution on CPU (~45fps) | Lower accuracy; struggles with partial occlusions |
| **YOLOv8s (small)** | **Optimal balance of accuracy and speed** | **Chosen Implementation** |
| YOLOv8m (medium) | Highest accuracy | Computationally expensive; impractical for parallel CPU processing |
| RT-DETR | Strong performance in crowded scenes | Heavy footprint; poor CPU performance |
| MediaPipe | Extremely fast | Person detection only; lacks confidence scoring; poor at distance |

### AI Evaluation

When evaluating detection models for CPU-bound retail CCTV processing, AI analysis recommended YOLOv8s. The reasoning centered on retail environments exhibiting moderate crowd density (2-8 individuals) and occlusion levels that YOLOv8s navigates effectively. The AI suggested escalating to YOLOv8m only if occlusion accuracy proved insufficient during testing.

### Final Decision & Rationale

YOLOv8s was selected. The source clips are 1920×1080 at 15-30fps, processed at every second frame (effective 7-15fps) on the CPU. Initial testing with YOLOv8n yielded a 15-20% higher false-positive rate (misclassifying inanimate displays as people). YOLOv8s significantly reduced these errors while maintaining an acceptable processing speed (~22 seconds per 140-second clip on CPU). The confidence threshold was calibrated to 0.35 rather than the default 0.25, further reducing false positives without discarding valid detections.

**Rejected AI Proposal:** Initial AI suggestions included utilizing a Vision-Language Model (VLM) for zone classification by feeding frame crops to a model to ascertain zone presence. This approach was rejected. It would introduce severe API latency per frame, incur high computational costs, and lacks the determinism and auditability of polygon-based zone assignment. The VLM approach offers utility only when zone boundaries are ambiguous, which does not apply to this store layout.

---

## Decision 2: Event Schema Design

### The Core Problem

The database schema must concurrently support diverse query patterns: per-visitor session reconstruction, per-zone dwell analysis, conversion funnel computation, and anomaly detection. Over-normalizing introduces expensive query-time joins, while under-normalizing inflates storage overhead.

### Options Considered

**Option A — Flat events only**
Metrics are computed by scanning all raw events. Implementation is straightforward, but query performance degrades rapidly at scale.

**Option B — Events + materialized sessions (Chosen)**
Events serve as an immutable ledger. Sessions are constructed incrementally during ingestion. Endpoints query materialized sessions for aggregates and reference the event ledger for zone-level detail.

**Option C — Pre-aggregated metrics only**
Retains only computed metrics without raw event data. Yields high-speed queries but eliminates debugging capabilities and retroactive computation. This was rejected immediately as it violates data integrity principles.

### AI Evaluation

AI analysis endorsed Option B and proposed implementing a `session_seq` field within the metadata payload—an ordinal counter per visitor session. This mechanism permits the exact reconstruction of zone visit sequences without relying strictly on timestamps, which is critical when processing asynchronous camera feeds. 

The AI further recommended persisting the `is_staff` flag on every event rather than exclusively at the session level. This redundancy ensures that if staff classification algorithms improve, individual historical events can be re-evaluated without triggering a full re-ingestion cycle.

### Final Decision & Rationale

Option B was implemented with the following specifications:

- **`event_id` as UUID v4:** Guarantees global uniqueness and enables safe, idempotent ingest operations across independent pipeline executions.
- **`visitor_id` as a short hash:** Provides log readability and stability within a defined session.
- **`dwell_ms` on all events:** Applied universally (including ENTRY/EXIT events with `dwell_ms=0`), simplifying downstream consumers by eliminating event-type dependency checks.
- **Flat metadata structure:** Attributes such as `queue_depth`, `sku_zone`, and `session_seq` are grouped logically to maintain a clean top-level schema across all event variants.

---

## Decision 3: Storage Engine — SQLite over PostgreSQL

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| **SQLite** | Zero configuration; single file; native to container execution | Lacks concurrency for heavy multi-process writes |
| PostgreSQL | Production-grade; handles concurrent writes; comprehensive SQL support | Requires dedicated container and connection pooling |
| Redis | Exceptional in-memory performance | Lacks default persistence and relational query support |
| DuckDB | Optimized for analytical workloads | Unnecessary overhead for the current scale |

### AI Evaluation

AI evaluation strongly favored SQLite to satisfy the strict `docker compose up` acceptance requirement, which mandates zero manual configuration. The AI noted that while SQLite perfectly fulfills the hackathon constraints, it would require replacement in a production architecture scaling to 40 simultaneous stores.

Additionally, AI analysis flagged a specific technical limitation: SQLite lacks a native `UPSERT` command that SQLAlchemy can handle transparently. When event batches contain multiple updates for an identical session, naive inserts trigger `UNIQUE` constraint violations. The implemented solution utilizes an in-memory session cache per ingest batch, combined with `db.flush()` post-insertion, ensuring the SQLAlchemy identity map resolves duplicates prior to SQLite execution.

### Final Decision & Rationale

SQLite was chosen explicitly to meet the acceptance criteria and eliminate external dependencies. The deployment footprint prioritizes flawless execution over maximum throughput. Since the pipeline processes single cameras sequentially and pushes data in batches (up to 500 events), concurrent write bottlenecks are avoided.

**Conditions for Re-evaluation:** If the system scales to ingest concurrent real-time events across all 40 stores, SQLite will become a bottleneck. The migration path is trivialized by the ORM layer, requiring only a connection string update to PostgreSQL and the addition of a database service in `docker-compose.yml`.

---

## Decision 4: API Architecture — Synchronous FastAPI Ingest

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| **Synchronous FastAPI ingest** | Deterministic processing; simplified debugging; no worker coordination | Suboptimal for extreme throughput |
| Async background workers | Handles massive queues and long-running tasks efficiently | High orchestration overhead; complex failure modes |
| Streaming ingest pipeline | Near real-time data processing | Significant architectural overhead for batch workflows |

### Final Decision & Rationale

A synchronous FastAPI ingest architecture was selected. This guarantees that each request is processed end-to-end and persisted immediately. Given the constraints of the project—small batch sizes, bounded ingestion, and a strict requirement for reliability—synchronous processing eliminates the complexities of message queues, worker states, and retry orchestration.

**Conditions for Re-evaluation:** Should ingestion volume surge or continuous cross-store processing become a requirement, event processing would migrate to a background message queue, delegating FastAPI to a gateway role.

---

## Summary Table

| Decision | Chosen Implementation | Rejected Alternatives | Primary Rationale |
|----------|-----------------------|-----------------------|-------------------|
| Detection model | YOLOv8s | YOLOv8m, MediaPipe, VLM | Optimal CPU speed and accuracy balance |
| Zone classification | Polygon-based | VLM frame crops | Deterministic, zero-latency execution |
| Schema design | Events + materialized sessions | Flat events only | Superior query performance for endpoints |
| Storage | SQLite | PostgreSQL | Zero-configuration container execution |
| API architecture | Synchronous FastAPI ingest | Async workers, streaming | Predictable request handling and simplicity |
| Staff detection | Store-specific HSV profiles | Re-ID embeddings | Visual uniforms are established per store |

---

## Resource Contract Addendum

Following updates to the challenge resources, the implementation transitioned from a static schema contract to a dynamic compatibility layer.

### Event Schema Implementation
The detector retains the robust, uppercase challenge schema for raw video output (`ENTRY`, `EXIT`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON`, `REENTRY`). The API accommodates the updated lowercase sample-event schema via a boundary adapter implemented in `app/models.py`. This isolates schema normalization from the core metric logic.

### POS Parser Implementation
The updated POS dataset utilizes a line-item format (`order_id`, `order_date`, `order_time`, `total_amount`). The ingestion loader was upgraded to support both this structure and the legacy `transaction_id,timestamp,basket_value_inr` format. Records are aggregated by `order_id` prior to storage, enforcing conversion tracking at the purchase level rather than the SKU level.

### Dataset Discovery Logic
Given the variability in store ZIP filenames, the runner scripts now dynamically discover clip folders and infer camera roles based on nomenclature when a precise layout mapping is unavailable. This ensures pipeline resilience, allowing execution to proceed while polygon calibration is refined.

### Store Identity and Cross-Camera Tracking
Store 1 and Store 2 are normalized to `ST1008` and `ST1076` respectively, preserving raw dataset integrity while aligning with POS and event identities. The ingestion process resolves camera-local tracking by evaluating entry clips prior to floor and billing clips, creating temporal session links. This heuristic maximizes funnel accuracy within the batch processing constraints, avoiding the integration risks associated with deploying unverified appearance embedding models.

### Staff Profile Configuration
Staff detection profiles were updated to reflect store-specific characteristics. The detector applies a solid black uniform profile for Store 1 and a pink-top/black-bottom HSV profile for Store 2. This color-based classification remains the most effective technique for anonymized challenge footage.

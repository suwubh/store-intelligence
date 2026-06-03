# Store Intelligence API

End-to-end retail analytics from anonymised CCTV footage: clips under `dataset/clips/Store 1` and `Store 2` become structured events, events are ingested into a FastAPI service, and the API exposes live store metrics, funnel, heatmap, anomalies, and health.

Each store folder includes:

- CCTV clips (entry, floor/zone, billing)
- `store_layout.json` — camera `source_file` map + zone polygons (auto-generated; refine as needed)
- `*layout*.png` — floor plan reference image

Store IDs used by the API:

| Clip folder | `store_id`   |
|-------------|--------------|
| Store 1     | `ST1008`     |
| Store 2     | `ST1076`     |

## Quick Start

```powershell
# 1. Install dependencies
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. Validate datasets (Store 1 / Store 2 + POS + sample events)
python pipeline/validate_dataset.py --dataset dataset

# 3. Start the API
docker compose up --build

# 4. Ingest pre-generated pipeline events (ST1008 — Store 1 clips)
#    This step is required for metrics/funnel/heatmap to show real data.
#    Run from the project root after "docker compose up":
python -c "
import json, urllib.request, pathlib
for p in sorted(pathlib.Path('dataset/events').glob('ST1008_*.jsonl')):
    events = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    if not events: continue
    body = json.dumps({'events': events}).encode()
    req = urllib.request.Request('http://localhost:8000/events/ingest', body, {'Content-Type':'application/json'}, method='POST')
    print(p.name, urllib.request.urlopen(req).read().decode())
"

# 5. Ingest updated sample events (ST1076 demo schema)
python -c "import json, urllib.request; events=[json.loads(l) for l in open('dataset/events/sample_events.jsonl') if l.strip()]; req=urllib.request.Request('http://localhost:8000/events/ingest', json.dumps({'events': events}).encode(), {'Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(req).read().decode())"

# 6. Query metrics
curl http://localhost:8000/stores/ST1008/metrics
curl http://localhost:8000/stores/ST1076/funnel
```

> **Note on health status:** The `/health` endpoint reports `"degraded"` when the latest event timestamp is more than 10 minutes old. Since the pre-generated events in `dataset/events/` are from April 2026, the feed will show as stale after a fresh ingest. This is correct behaviour — the system is designed for real-time clips. Re-run the pipeline against live clips (`run_pipeline.py`) to get a live feed. The `last_event_timestamp` field in the health response shows when data was recorded; `event_count_last_hour` shows recent API activity.

## Raw Clip Processing

```powershell
# One store (layout is dataset/clips/Store 1/store_layout.json; emits ST1008)
python run_pipeline.py --store-folder "Store 1"

# Both stores
python run_pipeline.py --all-stores

# Process + ingest into running API
python run_pipeline.py --store-folder "Store 1" --api-url http://localhost:8000
```

Windows wrapper:

```powershell
pipeline\run.bat --store-folder "Store 1" --api-url http://localhost:8000
```

Events are written to `dataset/events/<store_id>_<camera>_events.jsonl`. The runner processes entry cameras first, then floor/zone cameras, then billing cameras so API ingestion can link camera-local tracks to the active entry sessions.

Regenerate layout JSON from clips (resolutions + filename → camera roles):

```powershell
python -c "from pathlib import Path; from pipeline.layout_builder import write_store_layout; write_store_layout(Path('dataset/clips/Store 1'))"
```

## API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /events/ingest` | Validates, deduplicates, and stores up to 500 events per batch |
| `GET /stores/{id}/metrics` | Unique visitors, conversion rate, dwell, queue depth, abandonment |
| `GET /stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase |
| `GET /stores/{id}/heatmap` | Zone visit frequency, average dwell, 0-100 score |
| `GET /stores/{id}/anomalies` | Queue spike, conversion drop, dead zone checks |
| `GET /health` | Service status, per-store last event time, stale-feed warning |

## Event Compatibility

The detection pipeline emits the **canonical uppercase schema** (`ENTRY`, `ZONE_ENTER`, …). The API also accepts the **updated sample-event schema** (`entry`, `zone_entered`, `queue_completed`, `id_token`, `store_code`, `track_id`). Rows without `event_id` get deterministic UUIDv5 IDs. `track_id` 101–103 link to `id_token` values when ingesting the bundled sample file.

## POS Correlation

`dataset/pos_transactions.csv` uses line-item rows aggregated by `order_id`. Conversion: a visitor with a billing visit at time **B** is converted if a POS transaction exists with timestamp **T** where **B ≤ T ≤ B + 5 minutes** (challenge rule).

Store IDs are normalized at ingest/query time. `Store 1` and `ST_STORE_1` map to `ST1008`; `Store 2`, `ST_STORE_2`, and `store_1076` map to `ST1076`. Set `POS_TIMEZONE_OFFSET_MINUTES` if POS and event timestamps use different bases.

## Calibration Notes

The bundled `store_layout.json` files contain runnable polygons for every camera, but the floor zones are coarse frame-space regions. For stronger zone scoring, refine the polygons against the layout PNG and visible camera frames before final submission. Staff profiles are store-specific: Store 1 uses black-uniform detection; Store 2 uses a pink-top/black-bottom profile.

## Tests

```powershell
pip install -r requirements.txt
pytest tests/ -v --cov=app --cov=pipeline --cov-report=term-missing
```

## Project Structure

```text
app/                  FastAPI, ingestion, metrics, funnel, heatmap, anomalies
pipeline/             Detection, layout_builder, validate_dataset, runners
dataset/clips/        Store 1, Store 2 (clips + per-store store_layout.json)
dataset/events/       Emitted JSONL + sample_events.jsonl
DESIGN.md             Architecture and AI-assisted decisions
CHOICES.md            Engineering decisions
docs/DESIGN.md        Copy of architecture notes
docs/CHOICES.md       Copy of engineering decisions
```

# Store Intelligence API

End-to-end retail analytics from anonymised CCTV footage: clips under `dataset/clips/Store 1` and `Store 2` become structured events, events are ingested into a FastAPI service, and the API exposes live store metrics, funnel, heatmap, anomalies, and health.

Each store folder includes:

- CCTV clips (entry, floor/zone, billing)
- `store_layout.json` — camera `source_file` map + zone polygons (auto-generated; refine as needed)
- `*layout*.png` — floor plan reference image

Store IDs used by the API:

| Clip folder | `store_id`   |
|-------------|--------------|
| Store 1     | `ST_STORE_1` |
| Store 2     | `ST_STORE_2` |

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

# 4. Ingest updated sample events (ST1076 demo schema)
python -c "import json, urllib.request; events=[json.loads(l) for l in open('dataset/events/sample_events.jsonl') if l.strip()]; req=urllib.request.Request('http://localhost:8000/events/ingest', json.dumps({'events': events}).encode(), {'Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(req).read().decode())"

# 5. Query metrics
curl http://localhost:8000/stores/ST1076/metrics
curl http://localhost:8000/stores/ST1076/funnel
```

## Raw Clip Processing

```powershell
# One store (layout is dataset/clips/Store 1/store_layout.json)
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

Events are written to `dataset/events/<store_id>_<camera>_events.jsonl`.

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

Align POS `store_id` with the store you are analysing (`ST_STORE_1`, `ST_STORE_2`, or `ST1076` for the sample JSONL). Set `POS_TIMEZONE_OFFSET_MINUTES` if POS and event timestamps use different bases.

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
docs/DESIGN.md        Architecture
docs/CHOICES.md       Engineering decisions
```

#!/bin/bash
# run.sh — process all CCTV clips → events → (optionally) ingest into API
# Usage: bash pipeline/run.sh [--api-url http://localhost:8000] [--store ST1008] [--device cpu]

set -e

API_URL=""
STORE_ID="ST1008"
DEVICE="cpu"
DATASET_DIR="dataset"
LAYOUT=""
CLIP_START="2026-04-10T10:00:00Z"   # Brigade Bangalore clips are from 10 Apr 2026

while [[ $# -gt 0 ]]; do
  case $1 in
    --api-url)    API_URL="$2";      shift 2 ;;
    --store)      STORE_ID="$2";     shift 2 ;;
    --device)     DEVICE="$2";       shift 2 ;;
    --dataset)    DATASET_DIR="$2";  shift 2 ;;
    --layout)     LAYOUT="$2";       shift 2 ;;
    --clip-start) CLIP_START="$2";   shift 2 ;;
    *) shift ;;
  esac
done

OUTPUT_DIR="$DATASET_DIR/events"
mkdir -p "$OUTPUT_DIR"

if [ -z "$LAYOUT" ] && [ -f "$DATASET_DIR/store_layout.json" ]; then
  LAYOUT="$DATASET_DIR/store_layout.json"
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  Store Intelligence Detection Pipeline   ║"
echo "╚══════════════════════════════════════════╝"
echo "  Store   : $STORE_ID"
echo "  Dataset : $DATASET_DIR"
echo "  Output  : $OUTPUT_DIR"
echo "  Layout  : ${LAYOUT:-none}"
echo "  Device  : $DEVICE"
echo "  ClipStart: $CLIP_START"
[ -n "$API_URL" ] && echo "  API     : $API_URL"
echo ""

STORE_DIR="$DATASET_DIR/clips/$STORE_ID"
if [ ! -d "$STORE_DIR" ]; then
  echo "ERROR: Clips directory not found: $STORE_DIR"
  exit 1
fi

TOTAL_EVENTS=0

mapfile -t VIDEO_FILES < <(find "$STORE_DIR" -maxdepth 1 \( -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" \) | sort)

if [ ${#VIDEO_FILES[@]} -eq 0 ]; then
  echo "ERROR: No video files found in $STORE_DIR"
  exit 1
fi

echo "Found ${#VIDEO_FILES[@]} clips in $STORE_DIR"
echo ""

for clip in "${VIDEO_FILES[@]}"; do
  filename=$(basename "$clip")

  # Map filename → camera ID via source_file in store_layout.json
  CAMERA_ID=$(python3 -c "
import json, sys
try:
    layout = json.load(open('$LAYOUT'))
    target = '$filename'.lower()
    for cam_id, cam_data in layout.get('cameras', {}).items():
        if cam_data.get('source_file', '').lower() == target:
            print(cam_id)
            sys.exit(0)
    print('')
except Exception as e:
    print('')
" 2>/dev/null)

  if [ -z "$CAMERA_ID" ]; then
    echo "  ⚠️  $filename: no camera mapping in store_layout.json — skipping"
    continue
  fi

  OUTPUT_FILE="$OUTPUT_DIR/${STORE_ID}_${CAMERA_ID}_events.jsonl"
  echo "  📹 $filename → $CAMERA_ID → $(basename $OUTPUT_FILE)"

  python3 -m pipeline.detect \
    --video "$clip" \
    --store "$STORE_ID" \
    --camera "$CAMERA_ID" \
    ${LAYOUT:+--layout "$LAYOUT"} \
    --output "$OUTPUT_FILE" \
    --clip-start "$CLIP_START" \
    ${API_URL:+--api-url "$API_URL"} \
    --device "$DEVICE"

  COUNT=$(wc -l < "$OUTPUT_FILE" 2>/dev/null || echo 0)
  echo "  ✅ $COUNT events written"
  TOTAL_EVENTS=$((TOTAL_EVENTS + COUNT))
done

echo ""
echo "═══════════════════════════════════════════"
echo "  Total events: $TOTAL_EVENTS"
echo "═══════════════════════════════════════════"

# Bulk ingest if API URL provided
if [ -n "$API_URL" ]; then
  echo ""
  echo "📡 Ingesting all events into API at $API_URL ..."
  python3 -c "
import json, glob
import urllib.request

api_url = '${API_URL}'
files = sorted(glob.glob('${OUTPUT_DIR}/*.jsonl'))
if not files:
    print('  No event files found.')
    exit()

for ef in files:
    events = []
    with open(ef) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    if not events:
        print(f'  (empty) {ef}')
        continue
    acc, dupes, errs = 0, 0, 0
    for i in range(0, len(events), 500):
        batch = events[i:i+500]
        payload = json.dumps({'events': batch}).encode()
        req = urllib.request.Request(
            f'{api_url}/events/ingest',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read())
                acc   += res.get('accepted', 0)
                dupes += res.get('duplicate', 0)
                errs  += res.get('rejected', 0)
        except Exception as e:
            print(f'  ⚠️  Ingest error for {ef}: {e}')
    import os
    print(f'  ✅ {os.path.basename(ef)}: {acc} accepted, {dupes} dupes, {errs} rejected')
"
fi

echo ""
echo "Next: start the API with   docker compose up"
echo "      then run dashboard:  python dashboard/live.py --store $STORE_ID --api ${API_URL:-http://localhost:8000}"

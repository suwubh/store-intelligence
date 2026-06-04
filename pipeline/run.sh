#!/bin/bash
# Process Store 1 / Store 2 clips (layout JSON in each store folder).
# Usage: bash pipeline/run.sh --store-folder "Store 1" [--api-url http://localhost:8000]

set -e

STORE_FOLDER=""
API_URL=""
DEVICE="auto"
DATASET="dataset"
CLIP_START=""
ALL_STORES=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --store-folder) STORE_FOLDER="$2"; shift 2 ;;
    --api-url)      API_URL="$2";      shift 2 ;;
    --device)       DEVICE="$2";       shift 2 ;;
    --dataset)      DATASET="$2";      shift 2 ;;
    --clip-start)   CLIP_START="$2";   shift 2 ;;
    --all-stores)   ALL_STORES=1;      shift ;;
    *) shift ;;
  esac
done

CMD=(python run_pipeline.py --dataset "$DATASET" --device "$DEVICE")
[[ -n "$STORE_FOLDER" ]] && CMD+=(--store-folder "$STORE_FOLDER")
[[ -n "$ALL_STORES" ]] && CMD+=(--all-stores)
[[ -n "$API_URL" ]] && CMD+=(--api-url "$API_URL")
[[ -n "$CLIP_START" ]] && CMD+=(--clip-start "$CLIP_START")

"${CMD[@]}"

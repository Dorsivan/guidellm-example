#!/usr/bin/env bash
set -euo pipefail

: "${RHOAI_ENDPOINT:?Set RHOAI_ENDPOINT to your model's /v1 URL}"
: "${RHOAI_TOKEN:?Set RHOAI_TOKEN to your RHOAI API token}"
: "${MODEL_NAME:=glm5.2}"
: "${DATASET:=datasets/prompts.jsonl}"
: "${DURATION:=120}"
: "${GUIDELLM_IMAGE:=ghcr.io/vllm-project/guidellm:stable}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${SCRIPT_DIR}/.."
RESULTS_DIR="${REPO_DIR}/results/custom-dataset-$(date +%Y%m%d-%H%M%S)"
mkdir -p "${RESULTS_DIR}"

DATASET_PATH="${REPO_DIR}/${DATASET}"

if [[ ! -f "${DATASET_PATH}" ]]; then
  echo "Error: Dataset file not found: ${DATASET_PATH}"
  echo "Available datasets:"
  ls "${REPO_DIR}/datasets/"
  exit 1
fi

EXT="${DATASET_PATH##*.}"
case "${EXT}" in
  jsonl|json) DATA_KIND="json_file" ;;
  csv)        DATA_KIND="csv_file" ;;
  txt|text)   DATA_KIND="text_file" ;;
  *)          echo "Unsupported file extension: .${EXT}"; exit 1 ;;
esac

echo "Running benchmark with custom dataset: ${DATASET}"
echo "Data kind: ${DATA_KIND}"

podman run --rm \
  -e RHOAI_ENDPOINT -e RHOAI_TOKEN -e MODEL_NAME \
  -v "${REPO_DIR}/datasets:/datasets:ro,z" \
  -v "${RESULTS_DIR}:/results:z" \
  "${GUIDELLM_IMAGE}" \
  run \
    --backend kind=openai_http,target="${RHOAI_ENDPOINT}",api_key="${RHOAI_TOKEN}",model="${MODEL_NAME}" \
    --data kind="${DATA_KIND}",path="/datasets/$(basename "${DATASET}")" \
    --profile kind=sweep,sweep_size=6 \
    --constraint kind=max_duration,seconds="${DURATION}" \
    --seed kind=static,value=42 \
    --output kind=json,path=/results/benchmark.json \
    --output kind=csv,path=/results/benchmark.csv \
    --output kind=html,path=/results/benchmark.html

echo "Benchmark complete. Results saved to ${RESULTS_DIR}"

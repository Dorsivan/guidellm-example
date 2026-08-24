#!/usr/bin/env bash
set -euo pipefail

: "${RHOAI_ENDPOINT:?Set RHOAI_ENDPOINT to your model's /v1 URL}"
: "${RHOAI_TOKEN:?Set RHOAI_TOKEN to your RHOAI API token}"
: "${MODEL_NAME:=glm5.2}"
: "${STREAMS:=8}"
: "${DURATION:=120}"
: "${GUIDELLM_IMAGE:=ghcr.io/vllm-project/guidellm:stable}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="${SCRIPT_DIR}/../results/concurrent-$(date +%Y%m%d-%H%M%S)"
mkdir -p "${RESULTS_DIR}"

echo "Running concurrent benchmark with ${STREAMS} streams against ${MODEL_NAME}"

podman run --rm \
  -e RHOAI_ENDPOINT -e RHOAI_TOKEN -e MODEL_NAME \
  -v "${RESULTS_DIR}:/results:z" \
  "${GUIDELLM_IMAGE}" \
  run \
    --backend kind=openai_http,target="${RHOAI_ENDPOINT}",api_key="${RHOAI_TOKEN}",model="${MODEL_NAME}" \
    --data kind=synthetic_text,prompt_tokens=256,output_tokens=128 \
    --profile kind=concurrent,streams="${STREAMS}" \
    --constraint kind=max_duration,seconds="${DURATION}" \
    --seed kind=static,value=42 \
    --output kind=json,path=/results/benchmark.json \
    --output kind=csv,path=/results/benchmark.csv \
    --output kind=html,path=/results/benchmark.html

echo "Benchmark complete. Results saved to ${RESULTS_DIR}"

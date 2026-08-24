# GuideLLM Load Testing Example — GLM 5.2 on RHOAI

Load test the **GLM 5.2** model served via Red Hat OpenShift AI (RHOAI) using [GuideLLM](https://github.com/vllm-project/guidellm), the vLLM project's benchmarking toolkit.

## What GuideLLM Measures

GuideLLM reports the following metrics across all benchmark profiles:

| Metric | Description |
|--------|-------------|
| **TTFT** (Time to First Token) | Latency from request submission to first generated token (p50, p90, p99) |
| **ITL** (Inter-Token Latency) | Time between consecutive generated tokens (p50, p90, p99) |
| **E2E Latency** | Total request duration from start to finish (p50, p90, p99) |
| **Throughput (req/s)** | Completed requests per second |
| **Prompt Tokens/s** | Input token processing rate |
| **Output Tokens/s** | Token generation rate |
| **Total Tokens/s** | Combined prompt + output token throughput |
| **Token Counts** | Prompt and output token distributions per request |
| **Error Rate** | Percentage of failed requests |
| **Concurrency** | Number of in-flight requests during the benchmark |

Reports are generated in JSON, CSV, and HTML formats. The HTML report includes interactive charts for latency distributions and throughput curves.

## Prerequisites

- `podman` or `docker`
- Access to a GLM 5.2 model served on RHOAI (OpenAI-compatible `/v1/chat/completions` endpoint)
- An API token for the RHOAI inference endpoint

## Container Image

All scripts in this repo run GuideLLM via its container image:

```
ghcr.io/vllm-project/guidellm:stable
```

> **Important: use `stable`, not `latest`.** The `latest` tag may point to a pre-release
> that is **amd64-only**. The `stable` tag (and pinned `vX.Y.Z` tags from v0.7.0+) are
> multi-arch manifests that support both **linux/amd64** and **linux/arm64**.
> If you see `exec format error` or `image platform does not match`, switch to `stable`
> or a specific release tag. See [issue #498](https://github.com/vllm-project/guidellm/issues/498).

## Quick Start

```bash
# Set your RHOAI endpoint and token
export RHOAI_ENDPOINT="https://glm5-2-predictor-my-project.apps.cluster.example.com/v1"
export RHOAI_TOKEN="sha256~your-token-here"
export MODEL_NAME="glm5.2"

# Run a quick 60-second sweep benchmark with synthetic data
podman run --rm \
  -e RHOAI_ENDPOINT -e RHOAI_TOKEN -e MODEL_NAME \
  -v "$(pwd)/results:/results:z" \
  ghcr.io/vllm-project/guidellm:stable \
  run \
    --backend kind=openai_http,target="${RHOAI_ENDPOINT}",api_key="${RHOAI_TOKEN}",model="${MODEL_NAME}" \
    --data kind=synthetic_text,prompt_tokens=256,output_tokens=128 \
    --profile kind=sweep,sweep_size=6 \
    --constraint kind=max_duration,seconds=60 \
    --output kind=json,path=/results/sweep.json \
    --output kind=csv,path=/results/sweep.csv \
    --output kind=html,path=/results/sweep.html
```

## Repository Structure

```
.
├── README.md
├── datasets/
│   ├── prompts.jsonl          # Custom prompts in JSONL format
│   ├── prompts.csv            # Same prompts in CSV format
│   ├── coding-tasks.jsonl     # Domain-specific coding prompts
│   └── multiturn-conversations.jsonl  # Multi-turn chat histories
├── scripts/
│   ├── run-sweep.sh           # Full sweep benchmark (sync → throughput → interpolated rates)
│   ├── run-constant-rate.sh   # Fixed request rate benchmark
│   ├── run-throughput.sh      # Max throughput discovery
│   ├── run-custom-dataset.sh  # Benchmark with your own dataset
│   ├── run-concurrent.sh      # Fixed concurrency benchmark
│   └── run-multiturn.sh       # Multi-turn conversation benchmark
└── results/                   # Output directory (gitignored)
```

## Benchmark Profiles

### Sweep (recommended first run)

Runs synchronous → throughput → interpolated async rates in one pass. Gives you a full performance curve.

```bash
./scripts/run-sweep.sh
```

### Constant Rate

Send requests at a fixed rate (e.g., 5 req/s). Useful for testing against a known SLO.

```bash
RATE=5 ./scripts/run-constant-rate.sh
```

### Throughput

Discover the server's maximum throughput by saturating it with parallel requests.

```bash
./scripts/run-throughput.sh
```

### Concurrent

Keep a fixed number of requests in flight at all times.

```bash
STREAMS=8 ./scripts/run-concurrent.sh
```

## Using Custom Datasets

GuideLLM supports JSONL, CSV, JSON, text files, HuggingFace datasets, and more. This repo includes example datasets you can modify or replace with your own.

### JSONL Format (recommended)

Each line is a JSON object with a `prompt` field:

```json
{"prompt": "Explain the concept of transfer learning in deep learning.", "output_tokens_count": 256}
{"prompt": "Write a Python function that implements binary search.", "output_tokens_count": 128}
```

The `output_tokens_count` field is optional — it tells GuideLLM how many tokens to request via `max_tokens`.

### CSV Format

```csv
prompt,output_tokens_count
"Explain the concept of transfer learning in deep learning.",256
"Write a Python function that implements binary search.",128
```

### Run with your own dataset

Mount your dataset into the container and reference the in-container path:

```bash
# JSONL
podman run --rm \
  -e RHOAI_ENDPOINT -e RHOAI_TOKEN -e MODEL_NAME \
  -v "$(pwd)/datasets:/datasets:ro,z" \
  -v "$(pwd)/results:/results:z" \
  ghcr.io/vllm-project/guidellm:stable \
  run \
    --backend kind=openai_http,target="${RHOAI_ENDPOINT}",api_key="${RHOAI_TOKEN}",model="${MODEL_NAME}" \
    --data kind=json_file,path=/datasets/prompts.jsonl \
    --profile kind=sweep,sweep_size=6 \
    --constraint kind=max_duration,seconds=60 \
    --output kind=json,path=/results/benchmark.json \
    --output kind=csv,path=/results/benchmark.csv \
    --output kind=html,path=/results/benchmark.html

# CSV
podman run --rm \
  -e RHOAI_ENDPOINT -e RHOAI_TOKEN -e MODEL_NAME \
  -v "$(pwd)/datasets:/datasets:ro,z" \
  -v "$(pwd)/results:/results:z" \
  ghcr.io/vllm-project/guidellm:stable \
  run \
    --backend kind=openai_http,target="${RHOAI_ENDPOINT}",api_key="${RHOAI_TOKEN}",model="${MODEL_NAME}" \
    --data kind=csv_file,path=/datasets/prompts.csv \
    --profile kind=sweep,sweep_size=6 \
    --constraint kind=max_duration,seconds=60 \
    --output kind=json,path=/results/benchmark.json

# HuggingFace dataset (no mount needed)
podman run --rm \
  -e RHOAI_ENDPOINT -e RHOAI_TOKEN -e MODEL_NAME \
  -v "$(pwd)/results:/results:z" \
  ghcr.io/vllm-project/guidellm:stable \
  run \
    --backend kind=openai_http,target="${RHOAI_ENDPOINT}",api_key="${RHOAI_TOKEN}",model="${MODEL_NAME}" \
    --data kind=huggingface,source=garage-bAInd/Open-Platypus \
    --profile kind=sweep,sweep_size=6 \
    --constraint kind=max_duration,seconds=60 \
    --output kind=json,path=/results/benchmark.json
```

### Custom Column Mapping

If your dataset uses non-standard column names, use `--data-column-mapper`:

```bash
podman run --rm \
  -e RHOAI_ENDPOINT -e RHOAI_TOKEN -e MODEL_NAME \
  -v "$(pwd):/workspace:ro,z" \
  -v "$(pwd)/results:/results:z" \
  ghcr.io/vllm-project/guidellm:stable \
  run \
    --backend kind=openai_http,target="${RHOAI_ENDPOINT}",api_key="${RHOAI_TOKEN}",model="${MODEL_NAME}" \
    --data kind=json_file,path=/workspace/my-data.jsonl \
    --data-column-mapper '{"kind":"generative_column_mapper","column_mappings":{"text_column":"user_query","output_tokens_count_column":"max_tokens"}}' \
    --profile kind=throughput \
    --constraint kind=max_requests,count=500 \
    --output kind=json,path=/results/benchmark.json
```

## Environment Variables

Create a `.env` file (gitignored) or export these before running:

```bash
export RHOAI_ENDPOINT="https://glm5-2-predictor-my-project.apps.cluster.example.com/v1"
export RHOAI_TOKEN="sha256~your-token-here"
export MODEL_NAME="glm5.2"
```

## Air-Gapped / Closed Network Usage

If your environment cannot reach HuggingFace (e.g., air-gapped clusters), set these environment variables to prevent GuideLLM from attempting tokenizer downloads:

```bash
export TRANSFORMERS_OFFLINE=1
export HF_HUB_OFFLINE=1
export GUIDELLM__PREFERRED_PROMPT_TOKENS_SOURCE=server
export GUIDELLM__PREFERRED_OUTPUT_TOKENS_SOURCE=server
```

With file-based datasets (`json_file`, `csv_file`), token counts come from your `output_tokens_count` fields and the server's usage stats, so no tokenizer is needed.

For synthetic datasets, you can either:
- Pre-download a tokenizer and mount it into the container
- Use `--tokenizer '{"kind": "huggingface_auto", "model": "gpt2"}'` if gpt2 is cached locally

Add `-e TRANSFORMERS_OFFLINE=1 -e HF_HUB_OFFLINE=1` to all `podman run` commands in this scenario.

## Interpreting Results

After a sweep, check the HTML report (`results/sweep.html`) for visual charts. Key things to look for:

- **TTFT p99 vs. concurrency**: shows when the server starts queuing requests
- **Output tokens/s vs. request rate**: the throughput curve — find the knee where adding more load stops helping
- **ITL distribution**: consistent ITL means stable generation; a long tail means contention
- **Error rate**: any non-zero error rate at a given concurrency level signals you've exceeded capacity

The CSV output is ready for spreadsheet analysis or import into Grafana/BI tools.

## Multi-Turn Conversation Testing

GuideLLM supports multi-turn benchmarks to simulate realistic chat workloads. This is useful for measuring how your model handles conversation context and how the serving infrastructure manages KV cache across turns.

### Custom multi-turn dataset

The `datasets/multiturn-conversations.jsonl` file contains multi-turn conversations using **turn-suffixed columns**:

```json
{"prefix": "You are a helpful assistant.", "prompt_0": "What is KV cache?", "output_tokens_count_0": 256, "prompt_1": "How does PagedAttention improve it?", "output_tokens_count_1": 256}
```

Each turn gets its own numbered column: `prompt_0`, `prompt_1`, `prompt_2`, etc. (hyphens like `prompt-0` also work). Optionally add `output_tokens_count_0`, `output_tokens_count_1` to control response length per turn. The `prefix` field sets the system message.

GuideLLM captures the model's response from each turn and includes it as conversation history in subsequent turns, building a proper `[system, user, assistant, user, assistant, ...]` messages array — so the model sees the full conversation context, just like a real chat.

Run it with:

```bash
podman run --rm \
  -e RHOAI_ENDPOINT -e RHOAI_TOKEN -e MODEL_NAME \
  -v "$(pwd)/datasets:/datasets:ro,z" \
  -v "$(pwd)/results:/results:z" \
  ghcr.io/vllm-project/guidellm:stable \
  run \
    --backend kind=openai_http,target="${RHOAI_ENDPOINT}",api_key="${RHOAI_TOKEN}",model="${MODEL_NAME}" \
    --data '{"kind": "json_file", "path": "/datasets/multiturn-conversations.jsonl", "load_kwargs": {"split": "train"}}' \
    --data-preprocessor kind=turn_pivot \
    --profile kind=sweep,sweep_size=6 \
    --constraint kind=max_duration,seconds=60 \
    --output kind=json,path=/results/benchmark.json \
    --output kind=csv,path=/results/benchmark.csv \
    --output kind=html,path=/results/benchmark.html
```

> **Note:** You must pass `"load_kwargs": {"split": "train"}` to avoid the `DatasetDict has no attribute 'info'` error, and `--data-preprocessor kind=turn_pivot` to properly pivot the turn columns.

Or using the script:

```bash
DATASET=datasets/multiturn-conversations.jsonl ./scripts/run-custom-dataset.sh
```

### Synthetic multi-turn

Generate multi-turn conversations on the fly with the `turns` parameter (requires a tokenizer):

```bash
TURNS=4 ./scripts/run-multiturn.sh
```

### What to look for

- **TTFT increases across turns**: as the conversation grows, the prompt includes all prior turns (user + assistant messages), so TTFT should increase proportionally. A sharp spike may indicate prefix caching is not working.
- **ITL stability**: inter-token latency should stay relatively consistent regardless of conversation length.
- **Throughput drop at higher turn counts**: more turns means more tokens per request, which reduces throughput — quantify how much.
- **Token count growth**: later turns carry the full conversation history, so input token counts grow with each turn. Check `prompt_tokens` in the results to verify the model is receiving the expected context size.
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

- Python 3.10+
- Access to a GLM 5.2 model served on RHOAI (OpenAI-compatible `/v1/chat/completions` endpoint)
- An API token for the RHOAI inference endpoint

```bash
pip install "guidellm[recommended]"
```

## Quick Start

```bash
# Set your RHOAI endpoint and token
export RHOAI_ENDPOINT="https://glm5-2-predictor-my-project.apps.cluster.example.com/v1"
export RHOAI_TOKEN="sha256~your-token-here"
export MODEL_NAME="glm5.2"

# Run a quick 60-second sweep benchmark with synthetic data
guidellm run \
  --backend kind=openai_http,target=${RHOAI_ENDPOINT},api_key=${RHOAI_TOKEN},model=${MODEL_NAME} \
  --data kind=synthetic_text,prompt_tokens=256,output_tokens=128 \
  --profile kind=sweep,sweep_size=6 \
  --constraint kind=max_duration,seconds=60 \
  --output kind=json,path=results/sweep.json \
  --output kind=csv,path=results/sweep.csv \
  --output kind=html,path=results/sweep.html
```

## Repository Structure

```
.
├── README.md
├── datasets/
│   ├── prompts.jsonl          # Custom prompts in JSONL format
│   ├── prompts.csv            # Same prompts in CSV format
│   └── coding-tasks.jsonl     # Domain-specific coding prompts
├── scripts/
│   ├── run-sweep.sh           # Full sweep benchmark (sync → throughput → interpolated rates)
│   ├── run-constant-rate.sh   # Fixed request rate benchmark
│   ├── run-throughput.sh      # Max throughput discovery
│   ├── run-custom-dataset.sh  # Benchmark with your own dataset
│   └── run-concurrent.sh      # Fixed concurrency benchmark
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

```bash
# JSONL
guidellm run \
  --backend kind=openai_http,target=${RHOAI_ENDPOINT},api_key=${RHOAI_TOKEN},model=${MODEL_NAME} \
  --data kind=json_file,path=datasets/prompts.jsonl \
  --profile kind=sweep,sweep_size=6 \
  --constraint kind=max_duration,seconds=60

# CSV
guidellm run \
  --backend kind=openai_http,target=${RHOAI_ENDPOINT},api_key=${RHOAI_TOKEN},model=${MODEL_NAME} \
  --data kind=csv_file,path=datasets/prompts.csv \
  --profile kind=sweep,sweep_size=6 \
  --constraint kind=max_duration,seconds=60

# HuggingFace dataset
guidellm run \
  --backend kind=openai_http,target=${RHOAI_ENDPOINT},api_key=${RHOAI_TOKEN},model=${MODEL_NAME} \
  --data kind=huggingface,source=garage-bAInd/Open-Platypus \
  --profile kind=sweep,sweep_size=6 \
  --constraint kind=max_duration,seconds=60

# Plain text file (one prompt per line)
guidellm run \
  --backend kind=openai_http,target=${RHOAI_ENDPOINT},api_key=${RHOAI_TOKEN},model=${MODEL_NAME} \
  --data kind=text_file,path=my-prompts.txt \
  --profile kind=sweep,sweep_size=6 \
  --constraint kind=max_duration,seconds=60
```

### Custom Column Mapping

If your dataset uses non-standard column names, use `--data-column-mapper`:

```bash
guidellm run \
  --backend kind=openai_http,target=${RHOAI_ENDPOINT},api_key=${RHOAI_TOKEN},model=${MODEL_NAME} \
  --data kind=json_file,path=my-data.jsonl \
  --data-column-mapper '{"kind":"generative_column_mapper","column_mappings":{"text_column":"user_query","output_tokens_count_column":"max_tokens"}}' \
  --profile kind=throughput \
  --constraint kind=max_requests,count=500
```

## Environment Variables

Create a `.env` file (gitignored) or export these before running:

```bash
export RHOAI_ENDPOINT="https://glm5-2-predictor-my-project.apps.cluster.example.com/v1"
export RHOAI_TOKEN="sha256~your-token-here"
export MODEL_NAME="glm5.2"
```

## Interpreting Results

After a sweep, check the HTML report (`results/sweep.html`) for visual charts. Key things to look for:

- **TTFT p99 vs. concurrency**: shows when the server starts queuing requests
- **Output tokens/s vs. request rate**: the throughput curve — find the knee where adding more load stops helping
- **ITL distribution**: consistent ITL means stable generation; a long tail means contention
- **Error rate**: any non-zero error rate at a given concurrency level signals you've exceeded capacity

The CSV output is ready for spreadsheet analysis or import into Grafana/BI tools.

# GuideLLM Nightly Benchmarks

Kubernetes CronJob that runs GuideLLM benchmarks against GLM 5.2 on RHOAI every night at 2 AM UTC.

## What It Runs

1. **Synthetic sweep** — 256 prompt tokens / 128 output tokens, 6-step sweep (sync → throughput → interpolated rates), 120s per step
2. **Custom dataset sweep** — Same sweep profile using real prompts from `prompts.jsonl`

Results (JSON, CSV, HTML) are stored in a PVC under `/results/YYYY-MM-DD/`.

## Setup

1. **Edit the Secret** with your real RHOAI endpoint and token:

   ```bash
   cp secret.yaml secret-real.yaml
   # Edit secret-real.yaml with your values
   ```

2. **Create the namespace and apply:**

   ```bash
   oc new-project guidellm-nightly
   oc apply -k .
   ```

3. **Trigger a manual run to verify:**

   ```bash
   oc create job guidellm-manual --from=cronjob/guidellm-nightly -n guidellm-nightly
   oc logs -f job/guidellm-manual -n guidellm-nightly
   ```

4. **View results:**

   ```bash
   # Find the results pod or attach a debug pod to the PVC
   oc run results-viewer --rm -it --image=registry.access.redhat.com/ubi9/ubi-minimal \
     --overrides='{"spec":{"containers":[{"name":"viewer","image":"registry.access.redhat.com/ubi9/ubi-minimal","command":["ls","-lR","/results"],"volumeMounts":[{"name":"results","mountPath":"/results"}]}],"volumes":[{"name":"results","persistentVolumeClaim":{"claimName":"guidellm-nightly-results"}}]}}' \
     -n guidellm-nightly
   ```

## Customization

- **Schedule**: Edit `spec.schedule` in `cronjob.yaml` (cron format, UTC)
- **Model**: Change `MODEL_NAME` env var in `cronjob.yaml`
- **Prompts**: Edit `configmap-dataset.yaml` to use your own JSONL prompts
- **Duration**: Adjust `--constraint kind=max_duration,seconds=120` in `configmap-script.yaml`
- **Sweep size**: Change `sweep_size=6` for more or fewer rate steps

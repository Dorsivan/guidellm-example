# GuideLLM Benchmark Pipeline: Scheduled (Cron) Runs

How to set up and run the GuideLLM benchmark pipeline as a nightly cron job on Red Hat OpenShift AI.

## Prerequisites

- OpenShift cluster with:
  - **Red Hat OpenShift AI** (RHOAI) operator installed
  - **OpenShift Pipelines** operator installed
- A namespace with a **DataSciencePipelinesApplication** (DSPA) deployed
- `oc` CLI logged in with appropriate permissions
- A model endpoint accessible from the cluster (vLLM, TGIS, or any OpenAI-compatible server)

## 1. Compile the Pipeline

```bash
pip install 'kfp>=2.12.1' kfp-kubernetes
python benchmark_pipeline.py
```

This produces `guidellm_benchmark_pipeline.yaml`. Rename it to use hyphens (DSPA requires lowercase alphanumeric names with `-` or `.`):

```bash
cp guidellm_benchmark_pipeline.yaml guidellm-benchmark-pipeline.yaml
```

## 2. Create Cluster Resources

### Results PVC

```bash
oc apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: guidellm-benchmark-results
  namespace: <namespace>
spec:
  accessModes:
  - ReadWriteMany
  resources:
    requests:
      storage: 5Gi
EOF
```

### Credentials Secret

```bash
oc apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: guidellm-rhoai-credentials
  namespace: <namespace>
type: Opaque
stringData:
  endpoint: "https://<model-endpoint-url>"
  token: "<api-token>"
EOF
```

### RBAC for LLMInferenceService (if using scale down/up)

The pipeline runner service account needs permission to patch LLMInferenceService resources:

```bash
oc apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: pipeline-llmisvc-access
  namespace: <namespace>
rules:
- apiGroups: ["serving.kserve.io"]
  resources: ["llminferenceservices"]
  verbs: ["get", "list", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: pipeline-llmisvc-access
  namespace: <namespace>
subjects:
- kind: ServiceAccount
  name: pipeline-runner-dspa
  namespace: <namespace>
roleRef:
  kind: Role
  name: pipeline-llmisvc-access
  apiGroup: rbac.authorization.k8s.io
EOF
```

## 3. Upload the Pipeline

```bash
DS_ROUTE=$(oc get dspa dspa -n <namespace> -o jsonpath='{.status.components.apiServer.externalUrl}')
TOKEN=$(oc whoami -t)

curl -sk -X POST "$DS_ROUTE/apis/v2beta1/pipelines/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "uploadfile=@guidellm-benchmark-pipeline.yaml" \
  -F "name=guidellm-benchmark-pipeline"
```

Save the `pipeline_id` and `pipeline_version_id` from the response.

To get the version ID after upload:

```bash
curl -sk "$DS_ROUTE/apis/v2beta1/pipelines/<pipeline_id>/versions" \
  -H "Authorization: Bearer $TOKEN"
```

## 4. Create the Scheduled (Cron) Run

> **IMPORTANT: DSPA uses 6-field cron format.**
>
> ```
> seconds  minutes  hours  day  month  dayOfWeek
> ```
>
> This is NOT the standard 5-field cron. If you use 5 fields, the first field
> is interpreted as **seconds** and everything shifts left, causing the schedule
> to fire at completely wrong times (e.g., every hour instead of once a day).
>
> | Intent              | Wrong (5-field)  | Correct (6-field)  |
> |---------------------|------------------|--------------------|
> | Daily at 10:00 PM   | `0 22 * * *`     | `0 0 22 * * *`     |
> | Daily at 4:30 PM    | `30 16 * * *`    | `0 30 16 * * *`    |
> | Daily at 2:15 AM    | `15 2 * * *`     | `0 15 2 * * *`     |
> | Weekdays at 9 AM    | `0 9 * * 1-5`    | `0 0 9 * * 1-5`    |

```bash
DS_ROUTE=$(oc get dspa dspa -n <namespace> -o jsonpath='{.status.components.apiServer.externalUrl}')
TOKEN=$(oc whoami -t)

curl -sk -X POST "$DS_ROUTE/apis/v2beta1/recurringruns" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "guidellm-nightly-benchmark",
    "pipeline_version_reference": {
      "pipeline_id": "<pipeline_id>",
      "pipeline_version_id": "<pipeline_version_id>"
    },
    "runtime_config": {
      "parameters": {
        "test_configs": "[{\"name\": \"small-128-64\", \"prompt_tokens\": 128, \"output_tokens\": 64, \"duration\": 120}, {\"name\": \"large-512-256\", \"prompt_tokens\": 512, \"output_tokens\": 256, \"duration\": 120}]",
        "redeploy_configmaps": "[]",
        "scale_targets": "[{\"name\": \"other-large-model\", \"namespace\": \"prod-ns\", \"replicas\": 0}]",
        "model_name": "my-model",
        "timeout_minutes": 480,
        "namespace": "<namespace>",
        "cleanup_configmap": ""
      }
    },
    "trigger": {
      "cron_schedule": {
        "cron": "0 0 22 * * *"
      }
    },
    "max_concurrency": 1,
    "mode": "ENABLE"
  }'
```

## 5. Pipeline Parameters Reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `test_configs` | JSON string | 3 default tests | Array of test configurations (see below) |
| `redeploy_configmaps` | JSON string | `"[]"` | ConfigMap names to apply between tests |
| `scale_targets` | JSON string | `"[]"` | LLMInferenceServices to scale down before tests |
| `model_name` | string | `"glm5.2"` | Model identifier for GuideLLM `--backend` |
| `timeout_minutes` | int | `120` | Total time budget; steps past deadline are skipped |
| `namespace` | string | `"guidellm-nightly"` | K8s namespace for redeployment/cleanup/scaling |
| `cleanup_configmap` | string | `""` | ConfigMap with YAML resource to delete after tests |

### Test config fields

```json
{
  "name": "my-test",
  "prompt_tokens": 256,
  "output_tokens": 128,
  "profile": "sweep",
  "sweep_size": 6,
  "rate": 1.0,
  "max_concurrency": 32,
  "streams": 8,
  "duration": 120,
  "request_format": "/v1/completions",
  "data": null
}
```

- `profile`: `sweep` (default), `constant_rate`, `throughput`, or `concurrent`
- `data`: raw GuideLLM `--data` arg; overrides `prompt_tokens`/`output_tokens` if set

### Scale targets format

```json
[
  {"name": "big-model", "namespace": "prod-ns", "replicas": 0},
  {"name": "medium-model", "replicas": 1}
]
```

- `namespace` defaults to the pipeline's `namespace` parameter if omitted
- Original replica counts are saved and restored after all tests complete
- A safety net in the finalize step restores replicas even if the pipeline crashes

### Redeploy configmaps format

```json
["redeploy-config-v2", ""]
```

- Entry 0 = applied between test 0 and test 1
- Empty string = skip redeployment for that slot
- Each ConfigMap must exist in the cluster and contain a YAML resource

## 6. Managing Scheduled Runs

All commands use the DSPA API:

```bash
DS_ROUTE=$(oc get dspa dspa -n <namespace> -o jsonpath='{.status.components.apiServer.externalUrl}')
TOKEN=$(oc whoami -t)
RR_ID="<recurring_run_id>"
```

**List all recurring runs:**
```bash
curl -sk "$DS_ROUTE/apis/v2beta1/recurringruns" -H "Authorization: Bearer $TOKEN"
```

**Check status:**
```bash
curl -sk "$DS_ROUTE/apis/v2beta1/recurringruns/$RR_ID" -H "Authorization: Bearer $TOKEN"
```

**Disable:**
```bash
curl -sk -X POST "$DS_ROUTE/apis/v2beta1/recurringruns/$RR_ID:disable" \
  -H "Authorization: Bearer $TOKEN"
```

**Re-enable:**
```bash
curl -sk -X POST "$DS_ROUTE/apis/v2beta1/recurringruns/$RR_ID:enable" \
  -H "Authorization: Bearer $TOKEN"
```

**List triggered runs:**
```bash
curl -sk "$DS_ROUTE/apis/v2beta1/runs" -H "Authorization: Bearer $TOKEN"
```

**Trigger a manual run (same params, no cron):**
```bash
curl -sk -X POST "$DS_ROUTE/apis/v2beta1/runs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "manual-benchmark-run",
    "pipeline_version_reference": {
      "pipeline_id": "<pipeline_id>",
      "pipeline_version_id": "<pipeline_version_id>"
    },
    "runtime_config": {
      "parameters": { ... }
    }
  }'
```

## 7. Pipeline DAG

```
setup → scale_down → test_0 → redeploy_0 → test_1 → ... → test_4 → scale_up
                                                                        ↓
                                                                 finalize (exit handler)
```

- **setup**: creates run directory, computes deadline
- **scale_down**: patches LLMInferenceService replicas down, saves originals to PVC
- **test_0..4**: runs GuideLLM benchmarks (unused slots no-op, timed-out slots skip)
- **redeploy_0..3**: applies ConfigMap YAML between tests, waits for model readiness
- **scale_up**: restores original replica counts from PVC
- **finalize**: always runs (exit handler); generates Markdown report, restores replicas if scale_up didn't run, optionally deletes cleanup resource

## Timezone

The DSPA scheduler uses UTC by default (configured via `CRON_SCHEDULE_TIMEZONE` env var on the scheduler deployment). All cron expressions are evaluated in UTC.

"""
GuideLLM Benchmark Pipeline for Red Hat OpenShift AI.

Each GuideLLM test runs as its own pipeline step (visible in the RHOAI UI).
Kubernetes operations (model redeployment, cleanup) run in separate steps
using a lightweight base image.

Pipeline DAG (for MAX_TEST_SLOTS=5):
  setup_run → scale_down → test_0 → redeploy_0 → ... → test_4 → scale_up
                                                                     ↓
                                                              finalize (exit handler, scale-up safety net)

Unused slots return immediately (no-op). Slots past the deadline return
"skipped". The finalize step always runs and produces a Markdown report.

Test configs are passed as a JSON array:
  [
    {"name": "small",  "prompt_tokens": 128, "output_tokens": 64,  "duration": 120},
    {"name": "large",  "prompt_tokens": 512, "output_tokens": 256, "duration": 120}
  ]

Redeployment between tests via ConfigMap references (length = num_tests - 1):
  ["redeploy-config-v2"]
  (empty string = no redeployment for that slot)

Compile:
    pip install 'kfp>=2.12.1' kfp-kubernetes
    python benchmark_pipeline.py

Upload guidellm_benchmark_pipeline.yaml to the OpenShift AI dashboard.
"""

import json
from typing import NamedTuple

from kfp import compiler, dsl
from kfp import kubernetes

# ---------------------------------------------------------------------------
# Compile-time constants (edit and recompile to change)
# ---------------------------------------------------------------------------
MAX_TEST_SLOTS = 5
RESULTS_PVC = "guidellm-benchmark-results"
CREDENTIALS_SECRET = "guidellm-rhoai-credentials"
GUIDELLM_IMAGE = "ghcr.io/vllm-project/guidellm:stable"
UBI_IMAGE = "registry.redhat.io/ubi9/python-311:latest"


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

@dsl.component(base_image=UBI_IMAGE)
def setup_run(
    test_configs_json: str,
    timeout_minutes: int,
) -> NamedTuple("SetupOutput", [("run_id", str), ("deadline_epoch", float)]):
    """Create the run directory and compute the deadline."""
    import json
    import os
    import time
    from collections import namedtuple
    from datetime import datetime, timezone

    configs = json.loads(test_configs_json)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    deadline = time.time() + timeout_minutes * 60

    run_dir = f"/mnt/results/{run_id}"
    os.makedirs(f"{run_dir}/status", exist_ok=True)

    metadata = {
        "run_id": run_id,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "test_configs": configs,
        "timeout_minutes": timeout_minutes,
        "deadline_epoch": deadline,
        "total_tests": len(configs),
    }
    with open(f"{run_dir}/metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"Run {run_id}: {len(configs)} tests, {timeout_minutes}min budget")

    Output = namedtuple("SetupOutput", ["run_id", "deadline_epoch"])
    return Output(run_id=run_id, deadline_epoch=deadline)


@dsl.component(base_image=GUIDELLM_IMAGE)
def run_single_test(
    test_configs_json: str,
    test_index: int,
    run_id: str,
    model_name: str,
    deadline_epoch: float,
) -> str:
    """Run one GuideLLM benchmark. Skips if index is out of range or past deadline."""
    import json
    import os
    import subprocess
    import time
    import traceback

    endpoint = os.environ["RHOAI_ENDPOINT"]
    api_key = os.environ["RHOAI_TOKEN"]

    configs = json.loads(test_configs_json)

    def write_status(status_dict):
        path = f"/mnt/results/{run_id}/status/test_{test_index}.json"
        with open(path, "w") as f:
            json.dump(status_dict, f, indent=2)
        return json.dumps(status_dict)

    if test_index >= len(configs):
        return write_status({"index": test_index, "status": "skipped", "reason": "no config"})

    cfg = configs[test_index]
    test_name = cfg.get("name", f"test-{test_index:02d}")

    remaining = deadline_epoch - time.time()
    if remaining <= 60:
        return write_status({
            "index": test_index, "name": test_name, "config": cfg,
            "status": "skipped", "reason": "timeout",
        })

    test_start = time.time()
    test_dir = f"/mnt/results/{run_id}/{test_name}"
    os.makedirs(test_dir, exist_ok=True)

    prompt_tokens = cfg.get("prompt_tokens", 256)
    output_tokens = cfg.get("output_tokens", 128)
    profile = cfg.get("profile", "sweep")
    sweep_size = cfg.get("sweep_size", 6)
    duration = min(cfg.get("duration", 120), max(int(remaining) - 30, 10))

    request_format = cfg.get("request_format", "/v1/completions")
    backend_arg = (
        f"kind=openai_http,target={endpoint},"
        f"api_key={api_key},model={model_name},"
        f"request_format={request_format}"
    )
    data_arg = cfg.get("data") or (
        f"kind=synthetic_text,"
        f"prompt_tokens={prompt_tokens},"
        f"output_tokens={output_tokens}"
    )

    if profile == "constant_rate":
        profile_arg = f"kind=constant,rate={cfg.get('rate', 1.0)},max_concurrency={cfg.get('max_concurrency', 32)}"
    elif profile == "throughput":
        profile_arg = f"kind=throughput,max_concurrency={cfg.get('max_concurrency', 32)}"
    elif profile == "concurrent":
        profile_arg = f"kind=concurrent,streams={cfg.get('streams', 8)}"
    else:
        profile_arg = f"kind=sweep,sweep_size={sweep_size}"

    cmd = [
        "guidellm", "run",
        "--backend", backend_arg,
        "--data", data_arg,
        "--profile", profile_arg,
        "--constraint", f"kind=max_duration,seconds={duration}",
        "--output", f"kind=json,path={test_dir}/benchmarks.json",
        "--output", f"kind=csv,path={test_dir}/benchmarks.csv",
        "--output", f"kind=html,path={test_dir}/benchmarks.html",
    ]

    print(f"TEST {test_index}: {test_name}")
    print(f"  tokens: {prompt_tokens} in / {output_tokens} out")
    print(f"  profile: {profile_arg}")
    print(f"  duration: {duration}s  (budget remaining: {int(remaining)}s)")

    exit_code = -1
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=duration + 120,
        )
        exit_code = result.returncode
        if result.stdout:
            print(result.stdout[-3000:])
        if exit_code != 0 and result.stderr:
            print(f"STDERR:\n{result.stderr[-1000:]}")
    except subprocess.TimeoutExpired:
        print("  subprocess timed out")
    except Exception:
        traceback.print_exc()

    status = "completed" if exit_code == 0 else "failed"

    # Extract key metrics from GuideLLM JSON output
    metrics = {}
    json_path = f"{test_dir}/benchmarks.json"
    if os.path.exists(json_path):
        try:
            with open(json_path) as f:
                data = json.load(f)
            benchmarks = (
                data if isinstance(data, list)
                else data.get("benchmarks", [data])
            )
            for idx, bench in enumerate(benchmarks):
                stats = bench.get("stats", bench)
                metrics[f"strategy_{idx}"] = {
                    k: stats[k]
                    for k in [
                        "request_rate", "request_concurrency",
                        "request_latency_seconds_mean", "request_latency_seconds_p99",
                        "time_to_first_token_seconds_mean", "time_to_first_token_seconds_p99",
                        "inter_token_latency_seconds_mean", "inter_token_latency_seconds_p99",
                        "output_tokens_per_second_mean", "total_tokens_per_second",
                        "completed_request_rate", "error_rate",
                    ]
                    if k in stats and stats[k] is not None
                }
        except Exception:
            pass

    elapsed = round(time.time() - test_start, 1)
    print(f"  -> {test_name}: {status} in {elapsed}s")

    return write_status({
        "index": test_index, "name": test_name, "config": cfg,
        "status": status, "exit_code": exit_code,
        "duration_seconds": elapsed, "metrics": metrics,
    })


@dsl.component(
    base_image=UBI_IMAGE,
    packages_to_install=["kubernetes>=28.0.0", "pyyaml>=6.0"],
)
def redeploy_model(
    redeploy_configmaps_json: str,
    redeploy_index: int,
    run_id: str,
    namespace: str,
    deadline_epoch: float,
) -> str:
    """Apply a ConfigMap's YAML to redeploy the model, then wait for readiness."""
    import json
    import os
    import ssl
    import time
    import urllib.request

    endpoint = os.environ["RHOAI_ENDPOINT"]
    api_key = os.environ["RHOAI_TOKEN"]

    configmaps = json.loads(redeploy_configmaps_json)

    def write_status(status_dict):
        status_dir = f"/mnt/results/{run_id}/status"
        os.makedirs(status_dir, exist_ok=True)
        with open(f"{status_dir}/redeploy_{redeploy_index}.json", "w") as f:
            json.dump(status_dict, f, indent=2)
        return json.dumps(status_dict)

    if redeploy_index >= len(configmaps):
        return write_status({"index": redeploy_index, "status": "skipped", "reason": "no config"})

    cm_name = configmaps[redeploy_index]
    if not cm_name or not cm_name.strip():
        return write_status({"index": redeploy_index, "status": "skipped", "reason": "empty"})

    remaining = deadline_epoch - time.time()
    if remaining <= 60:
        return write_status({"index": redeploy_index, "status": "skipped", "reason": "timeout"})

    cm_name = cm_name.strip()
    print(f"REDEPLOY {redeploy_index}: applying ConfigMap {cm_name}")

    try:
        from kubernetes import client, config as k8s_config
        from kubernetes.dynamic import DynamicClient
        import yaml

        k8s_config.load_incluster_config()
        v1 = client.CoreV1Api()
        cm = v1.read_namespaced_config_map(cm_name, namespace)

        yaml_content = None
        for key, value in (cm.data or {}).items():
            if key.endswith((".yaml", ".yml")):
                yaml_content = value
                break
        if not yaml_content and cm.data:
            yaml_content = list(cm.data.values())[0]
        if not yaml_content:
            return write_status({
                "index": redeploy_index, "status": "failed",
                "reason": f"no YAML data in ConfigMap {cm_name}",
            })

        resource = yaml.safe_load(yaml_content)
        dyn = DynamicClient(client.ApiClient())
        api = dyn.resources.get(
            api_version=resource["apiVersion"], kind=resource["kind"],
        )
        res_name = resource["metadata"]["name"]
        res_ns = resource["metadata"].get("namespace", namespace)

        try:
            existing = api.get(name=res_name, namespace=res_ns)
            resource["metadata"]["resourceVersion"] = existing.metadata.resourceVersion
            api.replace(body=resource, namespace=res_ns)
            print(f"  Updated {resource['kind']}/{res_name}")
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                api.create(body=resource, namespace=res_ns)
                print(f"  Created {resource['kind']}/{res_name}")
            else:
                raise
    except Exception as exc:
        return write_status({
            "index": redeploy_index, "status": "failed",
            "reason": f"apply error: {exc}",
        })

    # Wait for model readiness
    print("  Waiting for model readiness...")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ready = False
    for attempt in range(180):
        if time.time() >= deadline_epoch - 60:
            break
        try:
            req = urllib.request.Request(f"{endpoint}/models")
            req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                if resp.status == 200:
                    print(f"  Model ready after {(attempt + 1) * 10}s")
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(10)

    if not ready:
        return write_status({
            "index": redeploy_index, "status": "failed",
            "reason": "model not ready after redeployment",
        })

    return write_status({"index": redeploy_index, "status": "completed"})


@dsl.component(
    base_image=UBI_IMAGE,
    packages_to_install=["kubernetes>=28.0.0"],
)
def scale_down_models(
    scale_targets_json: str,
    run_id: str,
    namespace: str,
) -> str:
    """Scale down LLMInferenceService resources to free capacity for benchmarks."""
    import json
    import os

    targets = json.loads(scale_targets_json)
    if not targets:
        print("No scale targets specified, skipping")
        return json.dumps({"status": "skipped", "reason": "no targets"})

    from kubernetes import client, config as k8s_config
    from kubernetes.dynamic import DynamicClient

    k8s_config.load_incluster_config()
    dyn = DynamicClient(client.ApiClient())
    api = dyn.resources.get(kind="LLMInferenceService")

    originals = []
    results = []

    for target in targets:
        name = target["name"]
        ns = target.get("namespace", namespace)
        desired = target["replicas"]

        try:
            resource = api.get(name=name, namespace=ns)
            spec = resource.to_dict().get("spec", {})
            original = spec.get("replicas", 1)
            originals.append({"name": name, "namespace": ns, "replicas": original})

            api.patch(
                body={"spec": {"replicas": desired}},
                name=name, namespace=ns,
                content_type="application/merge-patch+json",
            )
            print(f"  Scaled {ns}/{name}: {original} -> {desired}")
            results.append({
                "name": name, "namespace": ns, "status": "scaled",
                "from": original, "to": desired,
            })
        except Exception as exc:
            print(f"  Failed to scale {ns}/{name}: {exc}")
            results.append({
                "name": name, "namespace": ns, "status": "failed",
                "reason": str(exc),
            })

    originals_path = f"/mnt/results/{run_id}/original_replicas.json"
    with open(originals_path, "w") as f:
        json.dump(originals, f, indent=2)
    print(f"Saved original replica counts to {originals_path}")

    return json.dumps({"status": "completed", "results": results})


@dsl.component(
    base_image=UBI_IMAGE,
    packages_to_install=["kubernetes>=28.0.0"],
)
def scale_up_models(
    run_id: str,
) -> str:
    """Restore LLMInferenceService resources to their original replica counts."""
    import json
    import os

    originals_path = f"/mnt/results/{run_id}/original_replicas.json"
    if not os.path.exists(originals_path):
        print("No original replicas file found, skipping")
        return json.dumps({"status": "skipped", "reason": "no originals file"})

    with open(originals_path) as f:
        originals = json.load(f)

    if not originals:
        return json.dumps({"status": "skipped", "reason": "empty originals"})

    from kubernetes import client, config as k8s_config
    from kubernetes.dynamic import DynamicClient

    k8s_config.load_incluster_config()
    dyn = DynamicClient(client.ApiClient())
    api = dyn.resources.get(kind="LLMInferenceService")

    results = []
    for entry in originals:
        name = entry["name"]
        ns = entry["namespace"]
        replicas = entry["replicas"]

        try:
            api.patch(
                body={"spec": {"replicas": replicas}},
                name=name, namespace=ns,
                content_type="application/merge-patch+json",
            )
            print(f"  Restored {ns}/{name} to {replicas} replicas")
            results.append({
                "name": name, "namespace": ns, "status": "restored",
                "replicas": replicas,
            })
        except Exception as exc:
            print(f"  Failed to restore {ns}/{name}: {exc}")
            results.append({
                "name": name, "namespace": ns, "status": "failed",
                "reason": str(exc),
            })

    marker_path = f"/mnt/results/{run_id}/scale_up_done"
    with open(marker_path, "w") as f:
        f.write("done")

    return json.dumps({"status": "completed", "results": results})


@dsl.component(
    base_image=UBI_IMAGE,
    packages_to_install=["kubernetes>=28.0.0", "pyyaml>=6.0"],
)
def finalize_run(
    namespace: str,
    cleanup_configmap: str,
    pvc_name: str,
    final_report: dsl.Output[dsl.Markdown],
) -> str:
    """Generate a final Markdown report and optionally clean up the model."""
    import glob
    import json
    import os
    from datetime import datetime, timezone

    results_dir = "/mnt/results"

    # Find the most recent run directory (by name, which is a timestamp)
    run_dirs = sorted(glob.glob(f"{results_dir}/*/metadata.json"), reverse=True)
    if run_dirs:
        run_dir = os.path.dirname(run_dirs[0])
        with open(run_dirs[0]) as f:
            metadata = json.load(f)
    else:
        metadata = {
            "run_id": "unknown", "test_configs": [], "total_tests": 0,
            "start_time": "?", "timeout_minutes": "?",
        }
        run_dir = results_dir

    run_id = metadata.get("run_id", "unknown")
    test_configs = metadata.get("test_configs", [])

    # Read all status files
    tests = []
    status_dir = f"{run_dir}/status"
    for i in range(len(test_configs)):
        status_path = f"{status_dir}/test_{i}.json"
        if os.path.exists(status_path):
            with open(status_path) as f:
                tests.append(json.load(f))
        else:
            tests.append({
                "index": i,
                "name": test_configs[i].get("name", f"test-{i:02d}"),
                "config": test_configs[i],
                "status": "crashed",
                "reason": "no status file (step may have been killed)",
            })

    redeploys = []
    for i in range(max(len(test_configs) - 1, 0)):
        status_path = f"{status_dir}/redeploy_{i}.json"
        if os.path.exists(status_path):
            with open(status_path) as f:
                redeploys.append(json.load(f))

    completed = [t for t in tests if t.get("status") == "completed"]
    failed = [t for t in tests if t.get("status") == "failed"]
    skipped = [t for t in tests if t.get("status") == "skipped"]
    crashed = [t for t in tests if t.get("status") == "crashed"]

    # Compute total duration from metadata start_time to now
    total_duration = "?"
    try:
        start = datetime.fromisoformat(metadata["start_time"])
        total_duration = f"{(datetime.now(timezone.utc) - start).total_seconds():.0f}"
    except Exception:
        pass

    timed_out = any(
        t.get("reason") == "timeout" for t in skipped
    )

    # ---- Build Markdown report ----------------------------------------

    lines = [
        f"# GuideLLM Benchmark Report: {run_id}\n",
        f"**Model:** `{test_configs[0].get('name', '?') if test_configs else '?'}`  ",
        f"**Started:** {metadata.get('start_time', '?')}  ",
        f"**Duration:** {total_duration}s  ",
        f"**Timeout budget:** {metadata.get('timeout_minutes', '?')} min  ",
        f"**Timed out:** {'Yes' if timed_out else 'No'}  ",
        f"**Completed:** {len(completed)} | **Failed:** {len(failed)} "
        f"| **Skipped:** {len(skipped)} | **Crashed:** {len(crashed)}  ",
        "",
    ]

    # Summary table
    if tests:
        lines.extend([
            "## Test Summary\n",
            "| # | Name | Tokens (in/out) | Profile | Status | Duration "
            "| Throughput (tok/s) | Latency p99 (s) | TTFT p99 (s) |",
            "|---|------|-----------------|---------|--------|----------"
            "|--------------------|-----------------|--------------|",
        ])
        for t in tests:
            tc = t.get("config", {})
            tokens = f"{tc.get('prompt_tokens', '?')}/{tc.get('output_tokens', '?')}"
            prof = tc.get("profile", "sweep")
            dur = f"{t['duration_seconds']:.0f}s" if t.get("duration_seconds") else "-"

            throughput = latency_p99 = ttft_p99 = "-"
            for _, sm in t.get("metrics", {}).items():
                if sm.get("total_tokens_per_second") is not None:
                    throughput = f"{sm['total_tokens_per_second']:.1f}"
                if sm.get("request_latency_seconds_p99") is not None:
                    latency_p99 = f"{sm['request_latency_seconds_p99']:.3f}"
                if sm.get("time_to_first_token_seconds_p99") is not None:
                    ttft_p99 = f"{sm['time_to_first_token_seconds_p99']:.3f}"
                break

            status_label = {
                "completed": "Completed", "failed": "Failed",
                "skipped": "Skipped", "crashed": "Crashed",
            }.get(t["status"], t["status"])

            lines.append(
                f"| {t.get('index', '?')+1 if isinstance(t.get('index'), int) else '?'} "
                f"| {t.get('name', '?')} | {tokens} | {prof} "
                f"| {status_label} | {dur} "
                f"| {throughput} | {latency_p99} | {ttft_p99} |"
            )
        lines.append("")

    # Redeployment summary
    active_redeploys = [r for r in redeploys if r.get("status") != "skipped"]
    if active_redeploys:
        lines.append("## Redeployments\n")
        for r in active_redeploys:
            idx = r.get("index", "?")
            st = r.get("status", "?")
            reason = r.get("reason", "")
            detail = f" ({reason})" if reason else ""
            lines.append(f"- Between test {idx} and {idx+1 if isinstance(idx, int) else '?'}: **{st}**{detail}")
        lines.append("")

    # Detailed metrics per completed test
    for t in completed:
        lines.append(f"### {t.get('name', '?')} (detailed)\n")
        metrics = t.get("metrics", {})
        if not metrics:
            lines.append("_No detailed metrics available._\n")
            continue
        lines.extend([
            "| Strategy | Rate (req/s) | Concurrency | Throughput (tok/s) "
            "| Latency mean (s) | Latency p99 (s) | TTFT mean (s) "
            "| TTFT p99 (s) | ITL mean (s) | Error rate |",
            "|----------|-------------|-------------|--------------------"
            "|--------------------|-----------------|---------------"
            "|--------------|--------------|------------|",
        ])
        for sname, sm in sorted(metrics.items()):
            def fmt(v):
                return f"{v:.4f}" if isinstance(v, float) else (
                    str(v) if v is not None else "-"
                )
            lines.append(
                f"| {sname} "
                f"| {fmt(sm.get('request_rate'))} "
                f"| {fmt(sm.get('request_concurrency'))} "
                f"| {fmt(sm.get('total_tokens_per_second'))} "
                f"| {fmt(sm.get('request_latency_seconds_mean'))} "
                f"| {fmt(sm.get('request_latency_seconds_p99'))} "
                f"| {fmt(sm.get('time_to_first_token_seconds_mean'))} "
                f"| {fmt(sm.get('time_to_first_token_seconds_p99'))} "
                f"| {fmt(sm.get('inter_token_latency_seconds_mean'))} "
                f"| {fmt(sm.get('error_rate'))} |"
            )
        lines.append("")

    # Failed / skipped / crashed details
    for label, group in [("Failed", failed), ("Skipped", skipped), ("Crashed", crashed)]:
        if group:
            lines.append(f"## {label} Tests\n")
            for t in group:
                lines.append(f"- **{t.get('name', '?')}**: {t.get('reason', '?')}")
            lines.append("")

    # Access instructions
    lines.extend([
        "## Accessing Detailed Results\n",
        f"Results are stored on PVC **`{pvc_name}`** under `/{run_id}/`.\n",
        "Each completed test directory contains:",
        "- `benchmarks.html` -- Interactive visual report",
        "- `benchmarks.json` -- Machine-readable results",
        "- `benchmarks.csv`  -- Spreadsheet-compatible data\n",
        "```bash",
        "# Start a viewer pod",
        f"oc run results-viewer --restart=Never \\",
        f"  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \\",
        f"  --overrides='{{\"spec\":{{\"containers\":[{{\"name\":\"v\","
        f"\"image\":\"registry.access.redhat.com/ubi9/ubi-minimal:latest\","
        f"\"command\":[\"sleep\",\"3600\"],"
        f"\"volumeMounts\":[{{\"name\":\"r\",\"mountPath\":\"/results\"}}]}}],"
        f"\"volumes\":[{{\"name\":\"r\","
        f"\"persistentVolumeClaim\":{{\"claimName\":\"{pvc_name}\"}}}}]}}}}'",
        "",
        "# Copy results locally",
        f"oc cp results-viewer:/results/{run_id}/ ./{run_id}/",
    ])
    for t in completed:
        lines.append(
            f"# oc cp results-viewer:/results/{run_id}/{t['name']}/benchmarks.html "
            f"./{t['name']}-report.html"
        )
    lines.extend([
        "",
        "oc delete pod results-viewer",
        "```",
        "",
    ])

    with open(final_report.path, "w") as f:
        f.write("\n".join(lines))

    # ---- Scale-up safety net (if scale_up_models step didn't run) -----

    scale_up_status = "not needed"
    marker = f"{run_dir}/scale_up_done"
    originals_path = f"{run_dir}/original_replicas.json"
    if os.path.exists(originals_path) and not os.path.exists(marker):
        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_cfg
            from kubernetes.dynamic import DynamicClient as DynClient

            k8s_cfg.load_incluster_config()
            dyn2 = DynClient(k8s_client.ApiClient())
            llm_api = dyn2.resources.get(kind="LLMInferenceService")

            with open(originals_path) as of:
                originals_list = json.load(of)

            restored = 0
            for entry in originals_list:
                try:
                    llm_api.patch(
                        body={"spec": {"replicas": entry["replicas"]}},
                        name=entry["name"], namespace=entry["namespace"],
                        content_type="application/merge-patch+json",
                    )
                    restored += 1
                    print(f"  Safety net: restored {entry['namespace']}/{entry['name']} to {entry['replicas']}")
                except Exception as exc2:
                    print(f"  Safety net: failed to restore {entry['namespace']}/{entry['name']}: {exc2}")

            scale_up_status = f"safety net restored {restored}/{len(originals_list)} services"
        except Exception as exc:
            scale_up_status = f"safety net error: {exc}"
    elif os.path.exists(marker):
        scale_up_status = "already completed by scale-up step"

    print(f"Scale-up: {scale_up_status}")

    # ---- Cleanup model ------------------------------------------------

    cleanup_status = "not requested"
    if cleanup_configmap and cleanup_configmap.strip():
        try:
            from kubernetes import client, config as k8s_config
            from kubernetes.dynamic import DynamicClient
            import yaml

            k8s_config.load_incluster_config()
            v1 = client.CoreV1Api()
            cm = v1.read_namespaced_config_map(cleanup_configmap.strip(), namespace)

            yaml_content = None
            for key, value in (cm.data or {}).items():
                if key.endswith((".yaml", ".yml")):
                    yaml_content = value
                    break
            if not yaml_content and cm.data:
                yaml_content = list(cm.data.values())[0]

            if yaml_content:
                resource = yaml.safe_load(yaml_content)
                dyn = DynamicClient(client.ApiClient())
                api = dyn.resources.get(
                    api_version=resource["apiVersion"],
                    kind=resource["kind"],
                )
                name = resource["metadata"]["name"]
                res_ns = resource["metadata"].get("namespace", namespace)
                api.delete(name=name, namespace=res_ns)
                cleanup_status = f"deleted {resource['kind']}/{name} in {res_ns}"
            else:
                cleanup_status = f"no YAML data in ConfigMap {cleanup_configmap}"
        except Exception as exc:
            cleanup_status = f"cleanup error: {exc}"

    print(f"Cleanup: {cleanup_status}")
    return cleanup_status


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@dsl.pipeline(
    name="GuideLLM Benchmark Pipeline",
    description=(
        "Sequential GuideLLM load tests with separate steps for each test "
        "and each redeployment. Supports up to {slots} test slots, timeout-"
        "aware graceful exit, a final Markdown report, and model cleanup."
    ).format(slots=MAX_TEST_SLOTS),
)
def guidellm_benchmark_pipeline(
    test_configs: str = json.dumps([
        {"name": "small-128-64",   "prompt_tokens": 128, "output_tokens": 64,  "duration": 120},
        {"name": "medium-256-128", "prompt_tokens": 256, "output_tokens": 128, "duration": 120},
        {"name": "large-512-256",  "prompt_tokens": 512, "output_tokens": 256, "duration": 120},
    ]),
    redeploy_configmaps: str = "[]",
    scale_targets: str = "[]",
    model_name: str = "glm5.2",
    timeout_minutes: int = 120,
    namespace: str = "guidellm-nightly",
    cleanup_configmap: str = "",
):
    """
    Args:
        test_configs: JSON array of test configurations (max {slots}).
            Each object may contain: name, prompt_tokens, output_tokens,
            profile (sweep | constant_rate | throughput | concurrent),
            sweep_size, rate, max_concurrency, streams, duration,
            data (raw GuideLLM --data arg).
        redeploy_configmaps: JSON array of ConfigMap names to apply
            between tests. Entry 0 = between test 0 and 1, etc.
            Use "" to skip redeployment for that slot.
        scale_targets: JSON array of LLMInferenceService objects to
            scale down before benchmarking and restore after.
            Each: {"name": str, "namespace"?: str, "replicas": int}.
            Namespace defaults to the pipeline namespace.
        model_name: Model identifier for the --backend argument.
        timeout_minutes: Total time budget. Steps past the deadline
            return "skipped" immediately.
        namespace: Kubernetes namespace for redeployment / cleanup.
        cleanup_configmap: ConfigMap containing a YAML resource to
            delete after all tests. Leave empty to skip cleanup.
    """.format(slots=MAX_TEST_SLOTS)

    # -- Setup: create run directory, compute deadline --------------------
    setup = setup_run(
        test_configs_json=test_configs,
        timeout_minutes=timeout_minutes,
    )
    setup.set_display_name("Setup")
    kubernetes.mount_pvc(setup, RESULTS_PVC, "/mnt/results")

    # -- Exit handler: report + cleanup (always runs) ---------------------
    finalize = finalize_run(
        namespace=namespace,
        cleanup_configmap=cleanup_configmap,
        pvc_name=RESULTS_PVC,
    )
    finalize.set_display_name("Final Report & Cleanup")
    kubernetes.mount_pvc(finalize, RESULTS_PVC, "/mnt/results")

    with dsl.ExitHandler(exit_task=finalize):
        # -- Scale down: free capacity for benchmarks --------------------
        sd = scale_down_models(
            scale_targets_json=scale_targets,
            run_id=setup.outputs["run_id"],
            namespace=namespace,
        )
        sd.set_display_name("Scale Down Models")
        sd.after(setup)
        kubernetes.mount_pvc(sd, RESULTS_PVC, "/mnt/results")

        prev_task = sd

        for i in range(MAX_TEST_SLOTS):
            # -- Test step ------------------------------------------------
            t = run_single_test(
                test_configs_json=test_configs,
                test_index=i,
                run_id=setup.outputs["run_id"],
                model_name=model_name,
                deadline_epoch=setup.outputs["deadline_epoch"],
            )
            t.set_display_name(f"Test {i}")
            t.after(prev_task)
            kubernetes.mount_pvc(t, RESULTS_PVC, "/mnt/results")
            kubernetes.use_secret_as_env(
                t,
                secret_name=CREDENTIALS_SECRET,
                secret_key_to_env={
                    "endpoint": "RHOAI_ENDPOINT",
                    "token": "RHOAI_TOKEN",
                },
            )
            t.set_cpu_request("2").set_cpu_limit("4")
            t.set_memory_request("4Gi").set_memory_limit("8Gi")

            # -- Redeploy step (between test i and i+1) -------------------
            if i < MAX_TEST_SLOTS - 1:
                r = redeploy_model(
                    redeploy_configmaps_json=redeploy_configmaps,
                    redeploy_index=i,
                    run_id=setup.outputs["run_id"],
                    namespace=namespace,
                    deadline_epoch=setup.outputs["deadline_epoch"],
                )
                r.set_display_name(f"Redeploy {i}")
                r.after(t)
                kubernetes.mount_pvc(r, RESULTS_PVC, "/mnt/results")
                kubernetes.use_secret_as_env(
                    r,
                    secret_name=CREDENTIALS_SECRET,
                    secret_key_to_env={
                        "endpoint": "RHOAI_ENDPOINT",
                        "token": "RHOAI_TOKEN",
                    },
                )
                prev_task = r
            else:
                prev_task = t

        # -- Scale up: restore original replicas -------------------------
        su = scale_up_models(
            run_id=setup.outputs["run_id"],
        )
        su.set_display_name("Scale Up Models")
        su.after(prev_task)
        kubernetes.mount_pvc(su, RESULTS_PVC, "/mnt/results")


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    compiler.Compiler().compile(
        pipeline_func=guidellm_benchmark_pipeline,
        package_path="guidellm_benchmark_pipeline.yaml",
    )
    print("Pipeline compiled to guidellm_benchmark_pipeline.yaml")

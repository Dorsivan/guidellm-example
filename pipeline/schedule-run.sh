#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: schedule-run.sh <command> [options]

Commands:
  list-pipelines                    List all pipelines
  list-experiments                  List all experiments
  create-run        [options]       Create a one-off pipeline run
  create-schedule   [options]       Create a recurring scheduled run

Common options:
  -N NAMESPACE       Namespace with the DSPA (or set KFP_NAMESPACE)
  -p PIPELINE_ID     Pipeline ID (required for create-run/create-schedule)
  -v VERSION_ID      Pipeline version ID (required for create-run/create-schedule)
  -e EXPERIMENT_ID   Experiment ID (required for create-run/create-schedule)

create-run / create-schedule options:
  -n NAME            Run display name (default: "benchmark-<timestamp>")
  -P KEY=VALUE       Pipeline parameter (repeatable). For JSON arrays, quote:
                       -P 'test_configs=[{"name":"small","prompt_tokens":128,...}]'

create-schedule only:
  -c CRON            Cron expression (default: "0 2 * * *" = daily at 02:00 UTC)
  -m MAX_CONCURRENCY Max concurrent runs (default: 1)

Environment:
  KFP_NAMESPACE      Namespace with the DSPA
  KFP_ENDPOINT       Override the API URL (skips route lookup)

Examples:
  # List pipelines to find your ID
  ./schedule-run.sh list-pipelines -N guidellm-nightly

  # One-off run with two test configs
  ./schedule-run.sh create-run -N guidellm-nightly \
    -p <pipeline-id> -v <version-id> -e <experiment-id> \
    -P 'test_configs=[
      {"name":"small","prompt_tokens":128,"output_tokens":64,"duration":120},
      {"name":"large","prompt_tokens":512,"output_tokens":256,"duration":120}
    ]' \
    -P timeout_minutes=90

  # Nightly schedule with redeployment between tests
  ./schedule-run.sh create-schedule -N guidellm-nightly \
    -p <pipeline-id> -v <version-id> -e <experiment-id> \
    -c "0 2 * * *" \
    -P 'test_configs=[
      {"name":"baseline","prompt_tokens":256,"output_tokens":128,"duration":120},
      {"name":"after-tune","prompt_tokens":256,"output_tokens":128,"duration":120}
    ]' \
    -P 'redeploy_configmaps=["model-v2-config"]' \
    -P cleanup_configmap=model-cleanup
EOF
    exit 1
}

NAMESPACE="${KFP_NAMESPACE:-}"
ENDPOINT="${KFP_ENDPOINT:-}"

resolve_endpoint() {
    if [[ -n "$ENDPOINT" ]]; then
        echo "$ENDPOINT"
        return
    fi
    if [[ -z "$NAMESPACE" ]]; then
        echo "Error: set KFP_NAMESPACE or pass -N <namespace>" >&2
        exit 1
    fi
    local host
    host=$(oc -n "$NAMESPACE" get route ds-pipeline-dspa -o jsonpath='{.spec.host}' 2>/dev/null) || {
        echo "Error: could not find DSPA route in namespace $NAMESPACE" >&2
        exit 1
    }
    echo "https://$host"
}

get_token() {
    oc whoami --show-token 2>/dev/null || {
        echo "Error: not logged in to OpenShift (run 'oc login' first)" >&2
        exit 1
    }
}

api() {
    local method="$1" path="$2" body="${3:-}"
    local url
    url="$(resolve_endpoint)$path"
    local token
    token="$(get_token)"

    local args=(-sk -H "Authorization: Bearer $token" -H "Content-Type: application/json")
    if [[ "$method" == "POST" ]]; then
        args+=(-X POST -d "$body")
    fi

    curl "${args[@]}" "$url"
}

param_to_json_value() {
    local val="$1"
    if echo "$val" | jq . >/dev/null 2>&1; then
        echo "$val"
    else
        jq -n --arg v "$val" '$v'
    fi
}

build_params_json() {
    local params_json="{}"
    for param in "$@"; do
        local key="${param%%=*}"
        local val="${param#*=}"
        local json_val
        json_val=$(param_to_json_value "$val")
        params_json=$(echo "$params_json" | jq --arg k "$key" --argjson v "$json_val" '. + {($k): $v}')
    done
    echo "$params_json"
}

cmd_list_pipelines() {
    while getopts "N:" opt; do
        case $opt in
            N) NAMESPACE="$OPTARG" ;;
            *) usage ;;
        esac
    done
    api GET "/apis/v2beta1/pipelines" | jq -r '
        .pipelines // [] | .[] |
        "\(.pipeline_id)\t\(.display_name)"
    ' | column -t -s $'\t'
}

cmd_list_experiments() {
    while getopts "N:" opt; do
        case $opt in
            N) NAMESPACE="$OPTARG" ;;
            *) usage ;;
        esac
    done
    api GET "/apis/v2beta1/experiments" | jq -r '
        .experiments // [] | .[] |
        "\(.experiment_id)\t\(.display_name)"
    ' | column -t -s $'\t'
}

cmd_create_run() {
    local pipeline_id="" version_id="" experiment_id="" run_name=""
    declare -a params=()

    while getopts "p:v:e:n:P:N:" opt; do
        case $opt in
            p) pipeline_id="$OPTARG" ;;
            v) version_id="$OPTARG" ;;
            e) experiment_id="$OPTARG" ;;
            n) run_name="$OPTARG" ;;
            P) params+=("$OPTARG") ;;
            N) NAMESPACE="$OPTARG" ;;
            *) usage ;;
        esac
    done

    if [[ -z "$pipeline_id" || -z "$version_id" || -z "$experiment_id" ]]; then
        echo "Error: -p, -v, and -e are required" >&2
        exit 1
    fi

    run_name="${run_name:-benchmark-$(date +%s)}"
    local params_json
    params_json=$(build_params_json "${params[@]+"${params[@]}"}")

    local body
    body=$(jq -n \
        --arg name "$run_name" \
        --arg exp_id "$experiment_id" \
        --arg pipe_id "$pipeline_id" \
        --arg ver_id "$version_id" \
        --argjson params "$params_json" \
        '{
            display_name: $name,
            experiment_id: $exp_id,
            pipeline_version_reference: {
                pipeline_id: $pipe_id,
                pipeline_version_id: $ver_id
            },
            runtime_config: {
                parameters: $params
            }
        }')

    local response
    response=$(api POST "/apis/v2beta1/runs" "$body")

    if echo "$response" | jq -e '.error' >/dev/null 2>&1; then
        echo "Error creating run:" >&2
        echo "$response" | jq . >&2
        exit 1
    fi

    local run_id
    run_id=$(echo "$response" | jq -r '.run_id // "unknown"')
    echo "Run created: $run_id ($run_name)"
}

cmd_create_schedule() {
    local pipeline_id="" version_id="" experiment_id="" run_name=""
    local cron="0 2 * * *" max_concurrency=1
    declare -a params=()

    while getopts "p:v:e:n:P:N:c:m:" opt; do
        case $opt in
            p) pipeline_id="$OPTARG" ;;
            v) version_id="$OPTARG" ;;
            e) experiment_id="$OPTARG" ;;
            n) run_name="$OPTARG" ;;
            P) params+=("$OPTARG") ;;
            N) NAMESPACE="$OPTARG" ;;
            c) cron="$OPTARG" ;;
            m) max_concurrency="$OPTARG" ;;
            *) usage ;;
        esac
    done

    if [[ -z "$pipeline_id" || -z "$version_id" || -z "$experiment_id" ]]; then
        echo "Error: -p, -v, and -e are required" >&2
        exit 1
    fi

    run_name="${run_name:-benchmark-nightly}"
    local params_json
    params_json=$(build_params_json "${params[@]+"${params[@]}"}")

    local body
    body=$(jq -n \
        --arg name "$run_name" \
        --arg exp_id "$experiment_id" \
        --arg pipe_id "$pipeline_id" \
        --arg ver_id "$version_id" \
        --arg cron "$cron" \
        --argjson max_conc "$max_concurrency" \
        --argjson params "$params_json" \
        '{
            display_name: $name,
            experiment_id: $exp_id,
            pipeline_version_reference: {
                pipeline_id: $pipe_id,
                pipeline_version_id: $ver_id
            },
            max_concurrency: $max_conc,
            mode: "ENABLE",
            trigger: {
                cron_schedule: {
                    cron: $cron
                }
            },
            runtime_config: {
                parameters: $params
            }
        }')

    local response
    response=$(api POST "/apis/v2beta1/recurringruns" "$body")

    if echo "$response" | jq -e '.error' >/dev/null 2>&1; then
        echo "Error creating schedule:" >&2
        echo "$response" | jq . >&2
        exit 1
    fi

    local recurring_run_id
    recurring_run_id=$(echo "$response" | jq -r '.recurring_run_id // "unknown"')
    echo "Recurring run created: $recurring_run_id ($run_name)"
    echo "  Schedule: $cron"
    echo "  Max concurrency: $max_concurrency"
}

# Parse global -N flag before the command
while [[ "${1:-}" == -N ]]; do
    NAMESPACE="$2"
    shift 2
done

command="${1:-}"
shift || true

case "$command" in
    list-pipelines)    cmd_list_pipelines "$@" ;;
    list-experiments)  cmd_list_experiments "$@" ;;
    create-run)        cmd_create_run "$@" ;;
    create-schedule)   cmd_create_schedule "$@" ;;
    *)                 usage ;;
esac

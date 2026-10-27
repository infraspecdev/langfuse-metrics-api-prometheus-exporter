# Langfuse Metrics API Prometheus Exporter

[![CI](https://github.com/infraspecdev/langfuse-metrics-api-prometheus-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/infraspecdev/langfuse-metrics-api-prometheus-exporter/actions/workflows/ci.yml)
[![Docker Hub](https://img.shields.io/docker/v/akshayakz2004/langfuse-metrics-api-prometheus-exporter?sort=semver&label=docker)](https://hub.docker.com/r/akshayakz2004/langfuse-metrics-api-prometheus-exporter)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Prometheus exporter for the [Langfuse Metrics API](https://langfuse.com/docs/metrics/features/metrics-api).
It turns the API's JSON query results into metrics that Prometheus or the OpenTelemetry
Collector can scrape, for dashboards and alerts in Grafana, SigNoz or any other backend.

It exports the **LLM analytics** Langfuse computes from your traces: cost, tokens,
latency, time to first token, errors and evaluation scores. It does not export metrics
about the Langfuse service itself, such as queue length.

## Contents

- [Why](#why)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Metrics](#metrics)
- [Custom queries](#custom-queries)
- [Limitations](#limitations)
- [Development](#development)
- [Releasing](#releasing)
- [License](#license)

## Why

Langfuse computes cost, token usage, latency and evaluation scores from your traces, and
the [Metrics API](https://langfuse.com/docs/metrics/features/metrics-api) is the only way
to read those numbers programmatically. The API doesn't speak a format that Prometheus or
the OpenTelemetry Collector accepts:

- **Every request needs a query.** The caller sends a JSON query that says what to group
  by, what to aggregate and which time range to cover. A Prometheus scrape is a plain
  `GET /metrics` with no parameters and no time range.
- **The response is JSON rows.** Results come back as rows such as
  `{"providedModelName": "gpt-4o", "sum_totalCost": 0.4213}`. Prometheus, and the
  Collector's Prometheus receiver, expect the
  [Prometheus text format](https://prometheus.io/docs/instrumenting/exposition_formats/):
  one line per sample with a metric name, labels and a value, such as
  `langfuse_total_cost_usd{model="gpt-4o"} 0.4213`.
- **Every request needs project credentials.** The API uses Basic auth with the key pair
  of one Langfuse project. Scrape targets usually need no auth.

Langfuse also doesn't push these metrics out over OTLP (it accepts OpenTelemetry traces as
input, but doesn't export metrics), and it has no Prometheus endpoint. So neither
Prometheus nor the Collector can collect them on its own.

This exporter sits in between. It runs the queries on a schedule for each project,
converts the rows into Prometheus gauges and serves them on `/metrics`, where Prometheus
or the Collector can scrape them like any other target.

Native Prometheus support for Langfuse is requested in
[langfuse#2508](https://github.com/orgs/langfuse/discussions/2508) and
[langfuse#3551](https://github.com/orgs/langfuse/discussions/3551). If it ships, prefer
it over this exporter.

## Features

- **Multiple projects** from one instance, each with its own API keys.
- **Config-driven.** Metrics are defined in `queries.json`. New apps, models or
  evaluators appear as new label values with no change at all.
- **Fast, cheap scrapes.** Queries run in the background; a scrape only reads the
  cached result.
- **Fails loudly.** Invalid config stops the exporter at startup instead of producing
  bad series.
- **Isolated failures.** One failing query doesn't affect the others, and every query
  reports its own health.
- **No dependencies.** Python standard library only; runs as non-root on a read-only
  filesystem.

## How it works

```
                  every REFRESH_SECONDS                      scrape
Langfuse  ◀──── GET /api/public/v2/metrics ────  exporter  ◀──────────  Prometheus / OTel Collector
          ────────────── JSON ─────────────────▶   :9464
```

Every `REFRESH_SECONDS`, the exporter runs each query in `queries.json` for each project
and each window in `WINDOWS`, converts the rows into Prometheus gauges and caches them.
`/metrics` serves the cache.

## Requirements

- A Langfuse instance (Cloud or self-hosted) that serves the v2 Metrics API,
  `GET /api/public/v2/metrics`. Tested with self-hosted Langfuse 4.46.
- A Langfuse API key pair (public and secret key) for each project you want to export.

## Quick start

### 1. Create API keys

In Langfuse, open **Project settings → API Keys** and create a key pair for each project.

### 2. Write `projects.json`

```json
[
  {"name": "chatbot", "public_key": "pk-lf-...", "secret_key": "sk-lf-..."},
  {"name": "search-assistant", "public_key": "pk-lf-...", "secret_key": "sk-lf-..."}
]
```

`name` becomes the `project` label on every metric. Keep this file out of version control.

### 3. Run

**Docker**

```sh
docker run --rm -p 9464:9464 \
  -e LANGFUSE_HOST=https://cloud.langfuse.com \
  -v "$PWD/projects.json:/etc/langfuse-exporter/projects.json:ro" \
  akshayakz2004/langfuse-metrics-api-prometheus-exporter:0.1.0
```

**Kubernetes**

```sh
kubectl -n langfuse create secret generic langfuse-metrics-api-exporter \
  --from-file=projects.json=./projects.json
kubectl apply -f examples/kubernetes.yaml
```

### 4. Check the output

Results appear after the first refresh, a few seconds after startup:

```sh
curl -s localhost:9464/metrics | grep langfuse_total_cost_usd
```

### 5. Scrape it

Point Prometheus ([examples/prometheus.yml](examples/prometheus.yml)) or the
OpenTelemetry Collector's `prometheus` receiver
([examples/otel-collector.yaml](examples/otel-collector.yaml)) at port `9464`. A 60s
scrape interval matches the default refresh; scraping more often returns the same values.

## Configuration

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `LANGFUSE_HOST` | `http://langfuse-web:3000` | Base URL of Langfuse |
| `PROJECTS_FILE` | `/etc/langfuse-exporter/projects.json` | Path to the projects file |
| `QUERIES_FILE` | `/app/queries.json` | Path to the queries file. The image includes a default set |
| `WINDOWS` | `86400` | Comma-separated rolling windows in seconds, e.g. `3600,86400` |
| `REFRESH_SECONDS` | `60` | How often all queries are re-run |
| `PORT` | `9464` | HTTP port |

### Endpoints

| Path | Description |
|---|---|
| `/metrics` | Metrics in Prometheus text format |
| `/healthz` | Returns `ok` while the server is running |

### Choosing windows

Each value is a total over a rolling window, for example cost in the last 24 hours,
and carries a `window` label such as `window="86400s"`. Values are gauges, not
counters: don't apply `rate()` or `increase()`, and select the window you need instead.

- `3600` (1 hour) suits alerting on projects with steady traffic.
- `86400` (24 hours) keeps dashboards populated when traffic is low.

Each refresh makes `projects × windows × queries` API calls, one after another. With the
7 default queries, 2 projects and 1 window, that is 14 calls per refresh.

## Metrics

Example output:

```
# HELP langfuse_total_cost_usd Total cost in USD in the window.
# TYPE langfuse_total_cost_usd gauge
langfuse_total_cost_usd{project="chatbot",window="86400s",environment="production",trace_name="answer-question",model="gpt-4o"} 0.4213
```

### Default metrics

Every metric has the labels `project` and `window`, plus the labels listed below.

| Metric | Labels | Description |
|---|---|---|
| `langfuse_observations` | `environment`, `trace_name`, `observation_type`, `observation_name`, `level` | Observations started. `level="ERROR"` counts errors |
| `langfuse_observation_latency_{avg,p50,p95,p99}_ms` | `environment`, `trace_name`, `observation_type`, `observation_name` | Observation latency |
| `langfuse_traces` | `environment`, `trace_name`, `level` | Traces started |
| `langfuse_trace_latency_{p50,p95}_ms` | `environment`, `trace_name`, `level` | End-to-end trace latency |
| `langfuse_{input,output,total}_tokens` | `environment`, `trace_name`, `model` | Tokens used by LLM calls |
| `langfuse_{input,output,total}_cost_usd` | `environment`, `trace_name`, `model` | Cost in USD. `0` for models Langfuse has no price for |
| `langfuse_generation_latency_sum_ms` | `environment`, `trace_name`, `model` | Total LLM call time |
| `langfuse_tokens_per_second_avg` | `environment`, `trace_name`, `model` | Average tokens per second |
| `langfuse_output_tokens_per_second_avg` | `environment`, `trace_name`, `model` | Average output tokens per second (streaming only) |
| `langfuse_time_to_first_token_{p50,p95}_ms` | `environment`, `trace_name`, `model` | Time to first token (streaming only) |
| `langfuse_numeric_scores` | `environment`, `score_name`, `score_source` | Numeric scores recorded |
| `langfuse_numeric_score_avg` | `environment`, `score_name`, `score_source` | Average numeric score |
| `langfuse_boolean_scores` | `environment`, `score_name`, `score_source` | Boolean scores recorded |
| `langfuse_boolean_score_true_ratio` | `environment`, `score_name`, `score_source` | Share of boolean scores that are true (0–1) |
| `langfuse_categorical_scores` | `environment`, `score_name`, `score_source`, `score_value` | Categorical scores recorded, by value |

### Exporter health

| Metric | Labels | Description |
|---|---|---|
| `langfuse_exporter_query_success` | `project`, `window`, `query` | `1` if the last run of the query succeeded, else `0` |
| `langfuse_exporter_query_duration_seconds` | `project`, `window`, `query` | Duration of the last run |
| `langfuse_exporter_query_rows` | `project`, `window`, `query` | Rows returned by the last run |
| `langfuse_exporter_last_refresh_timestamp_seconds` | | Unix time of the last completed refresh |

Recommended alerts:

- `langfuse_exporter_query_success == 0`: a query is failing.
- `time() - langfuse_exporter_last_refresh_timestamp_seconds > 300`: refreshes have stopped.

## Custom queries

Metrics are defined in `queries.json`. Each query is sent to the Metrics API, and each
row it returns becomes one sample per metric:

```json
{
  "queries": [
    {
      "key": "cost_by_model",
      "description": "Optional. For readers of this file only.",
      "view": "observations",
      "dimensions": [{"field": "providedModelName", "label": "model"}],
      "filters": [{"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}],
      "metrics": [
        {"aggregation": "sum", "measure": "totalCost", "name": "langfuse_total_cost_usd", "help": "Total cost in USD."}
      ]
    }
  ]
}
```

| Field | Description |
|---|---|
| `key` | Unique query name, used in the `query` label of the health metrics |
| `view`, `filters` | Passed to the Metrics API unchanged |
| `dimensions[].field` | Metrics API field to group by |
| `dimensions[].label` | Prometheus label name for that field. `project` and `window` are reserved |
| `metrics[].measure`, `metrics[].aggregation` | Metrics API measure and aggregation |
| `metrics[].name`, `metrics[].help` | Prometheus metric name and help text |

See the [Metrics API docs](https://langfuse.com/docs/metrics/features/metrics-api) for
the available views, fields, measures and aggregations.

The exporter validates label names, metric names and required fields at startup. Langfuse
validates views, fields and measures; an invalid one fails only that query, which shows
in the logs and in `langfuse_exporter_query_success`. Two queries can write to the same
metric only if they use the same labels.

To use your own file, mount it over `/app/queries.json`, or mount it elsewhere and set
`QUERIES_FILE`.

## Limitations

- **1000 rows per query.** This is the Metrics API's maximum. If a query returns 1000
  rows, results are truncated and a warning is logged. Avoid grouping by high-cardinality
  fields such as user or session ID.
- **Rolling-window gauges.** Values can't be re-aggregated over a different time range;
  add the windows you need to `WINDOWS`.
- **No traces view.** The v2 Metrics API has no traces view, so trace metrics are
  computed from root observations.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

pytest                                  # run tests
ruff check . && ruff format --check .   # lint and format check
docker build -t langfuse-exporter .     # build the image
```

## Releasing

1. In the GitHub repository settings, under **Secrets and variables → Actions**, set the
   variable `DOCKERHUB_USERNAME` and the secret `DOCKERHUB_TOKEN` (a Docker Hub access
   token with Read & Write scope).
2. Push a version tag:

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

The release workflow runs the tests and publishes
`akshayakz2004/langfuse-metrics-api-prometheus-exporter` with tags `0.1.0`, `0.1` and
`latest` for `linux/amd64` and `linux/arm64`.

## License

[Apache-2.0](LICENSE)

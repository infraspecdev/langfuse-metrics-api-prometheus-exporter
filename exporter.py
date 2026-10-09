"""Langfuse Metrics API (v2) to Prometheus exporter.

Every REFRESH_SECONDS it runs each query in QUERIES_FILE for each project in
PROJECTS_FILE and each window in WINDOWS, and serves the results as gauges on
:PORT/metrics. Each value is a total over its rolling window, not a counter.
"""

import base64
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000").rstrip("/")
PROJECTS_FILE = os.environ.get("PROJECTS_FILE", "/etc/langfuse-exporter/projects.json")
QUERIES_FILE = os.environ.get("QUERIES_FILE", "/app/queries.json")
WINDOWS = [int(w) for w in os.environ.get("WINDOWS", "86400").split(",") if w.strip()]
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "60"))
PORT = int(os.environ.get("PORT", "9464"))
ROW_LIMIT = 1000  # the API's maximum

PROM_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# The exporter adds these to every sample, so a query can't use them as labels.
RESERVED_LABELS = {"project", "window"}

log = logging.getLogger("langfuse-exporter")

_body = b""
_lock = threading.Lock()


def load_projects():
    with open(PROJECTS_FILE) as f:
        projects = json.load(f)
    for p in projects:
        token = base64.b64encode(f"{p['public_key']}:{p['secret_key']}".encode()).decode()
        p["auth"] = f"Basic {token}"
    return projects


def load_queries():
    """Rejects config mistakes at startup. Field, measure and aggregation names
    are left to Langfuse, which fails a bad query with a 400."""
    with open(QUERIES_FILE) as f:
        queries = json.load(f)["queries"]
    keys, types = set(), {}
    for q in queries:
        for field in ("key", "view", "dimensions", "filters", "metrics"):
            if field not in q:
                raise ValueError(f"query {q.get('key', '?')}: missing {field!r}")
        if q["key"] in keys:
            raise ValueError(f"query {q['key']}: duplicate key")
        keys.add(q["key"])
        label_names = [d["label"] for d in q["dimensions"]]
        for label in label_names:
            if not PROM_NAME.match(label) or label in RESERVED_LABELS:
                raise ValueError(f"query {q['key']}: invalid or reserved label {label!r}")
        if len(set(label_names)) != len(label_names):
            raise ValueError(f"query {q['key']}: duplicate label")
        if not q["metrics"]:
            raise ValueError(f"query {q['key']}: no metrics")
        for m in q["metrics"]:
            if not PROM_NAME.match(m["name"]):
                raise ValueError(f"query {q['key']}: invalid metric name {m['name']!r}")
            # Two queries may fill one gauge only with the same labels, or
            # Prometheus would see one metric with two label sets.
            if types.setdefault(m["name"], tuple(label_names)) != tuple(label_names):
                raise ValueError(f"metric {m['name']}: filled by queries with different labels")
    return queries


def run_query(project, q, start, end):
    body = {
        "view": q["view"],
        "dimensions": [{"field": d["field"]} for d in q["dimensions"]],
        "metrics": [{"measure": m["measure"], "aggregation": m["aggregation"]} for m in q["metrics"]],
        "filters": q["filters"],
        "timeDimension": None,
        "fromTimestamp": start,
        "toTimestamp": end,
        "orderBy": None,
        "config": {"row_limit": ROW_LIMIT},
    }
    url = f"{HOST}/api/public/v2/metrics?" + urllib.parse.urlencode({"query": json.dumps(body)})
    req = urllib.request.Request(url, headers={"Authorization": project["auth"]})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["data"]


def escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def labels(pairs):
    return "{" + ",".join(f'{k}="{escape(v)}"' for k, v in pairs) + "}"


def run_one(project, q, window, start_s, end_s, samples, status):
    t0 = time.monotonic()
    try:
        rows = run_query(project, q, start_s, end_s)
        ok = 1
    except Exception as e:  # one failing query shouldn't blank the rest
        log.warning("project=%s window=%s query=%s failed: %s", project["name"], window, q["key"], e)
        rows, ok = [], 0
    base = [("project", project["name"]), ("window", window), ("query", q["key"])]
    status.append(("langfuse_exporter_query_success", base, ok))
    status.append(("langfuse_exporter_query_duration_seconds", base, time.monotonic() - t0))
    status.append(("langfuse_exporter_query_rows", base, len(rows)))
    if len(rows) >= ROW_LIMIT:
        log.warning(
            "project=%s window=%s query=%s hit the %d row limit; results are truncated",
            project["name"],
            window,
            q["key"],
            ROW_LIMIT,
        )

    for row in rows:
        lbl = [("project", project["name"]), ("window", window)]
        lbl += [(d["label"], "" if row.get(d["field"]) is None else row[d["field"]]) for d in q["dimensions"]]
        for m in q["metrics"]:
            # Null when a row has nothing to measure, e.g. time to first
            # token on a non-streaming call: no sample rather than a 0.
            value = row.get(f"{m['aggregation']}_{m['measure']}")
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            samples.setdefault(m["name"], (m.get("help", ""), []))[1].append(f"{m['name']}{labels(lbl)} {value}")


def collect(projects, queries):
    end = datetime.now(timezone.utc)
    end_s = end.isoformat(timespec="seconds").replace("+00:00", "Z")

    samples = {}  # metric name -> (help, [lines])
    status = []
    for project in projects:
        for seconds in WINDOWS:
            start_s = (end - timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
            window = f"{seconds}s"
            for q in queries:
                run_one(project, q, window, start_s, end_s, samples, status)

    out = []
    for name, (help_, lines) in samples.items():
        out += [f"# HELP {name} {help_}", f"# TYPE {name} gauge", *lines]
    for name in sorted({s[0] for s in status}):
        out.append(f"# TYPE {name} gauge")
        out += [f"{n}{labels(lbl)} {v}" for n, lbl, v in status if n == name]
    out += [
        "# TYPE langfuse_exporter_last_refresh_timestamp_seconds gauge",
        f"langfuse_exporter_last_refresh_timestamp_seconds {time.time()}",
    ]
    return ("\n".join(out) + "\n").encode()


def refresh_loop(projects, queries):
    global _body
    while True:
        try:
            body = collect(projects, queries)
            with _lock:
                _body = body
        except Exception:
            log.exception("refresh failed")
        time.sleep(REFRESH_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            with _lock:
                body = _body
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        projects = load_projects()
        queries = load_queries()
        if not WINDOWS or min(WINDOWS) <= 0:
            raise ValueError(f"WINDOWS must list positive seconds, got {WINDOWS}")
    except Exception as e:
        # Exit rather than serve nothing, so the mistake is visible.
        log.error("bad config: %s", e)
        sys.exit(1)
    log.info(
        "exporting %d project(s) x %d window(s) %s x %d queries from %s, refresh=%ss",
        len(projects),
        len(WINDOWS),
        WINDOWS,
        len(queries),
        HOST,
        REFRESH_SECONDS,
    )
    threading.Thread(target=refresh_loop, args=(projects, queries), daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

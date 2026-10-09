import json
import urllib.error
from pathlib import Path

import pytest

import exporter

ROOT = Path(__file__).resolve().parent.parent
PROJECT = {"name": "demo", "auth": "Basic x"}


def query(**overrides):
    q = {
        "key": "q",
        "view": "observations",
        "dimensions": [{"field": "environment", "label": "environment"}],
        "filters": [],
        "metrics": [{"name": "m", "measure": "count", "aggregation": "count", "help": "M."}],
    }
    q.update(overrides)
    return q


@pytest.fixture
def queries_file(tmp_path, monkeypatch):
    def write(*queries):
        path = tmp_path / "queries.json"
        path.write_text(json.dumps({"queries": list(queries)}))
        monkeypatch.setattr(exporter, "QUERIES_FILE", str(path))

    return write


def test_bundled_queries_are_valid(monkeypatch):
    monkeypatch.setattr(exporter, "QUERIES_FILE", str(ROOT / "queries.json"))
    assert len(exporter.load_queries()) > 0


def test_missing_field_rejected(queries_file):
    q = query()
    del q["metrics"]
    queries_file(q)
    with pytest.raises(ValueError, match="missing"):
        exporter.load_queries()


@pytest.mark.parametrize("label", ["project", "window", "bad-label"])
def test_reserved_or_invalid_label_rejected(queries_file, label):
    queries_file(query(dimensions=[{"field": "environment", "label": label}]))
    with pytest.raises(ValueError, match="invalid or reserved label"):
        exporter.load_queries()


def test_invalid_metric_name_rejected(queries_file):
    queries_file(query(metrics=[{"name": "langfuse-cost", "measure": "count", "aggregation": "count"}]))
    with pytest.raises(ValueError, match="invalid metric name"):
        exporter.load_queries()


def test_metric_filled_with_different_labels_rejected(queries_file):
    other = query(key="other", dimensions=[{"field": "name", "label": "name"}])
    queries_file(query(), other)
    with pytest.raises(ValueError, match="different labels"):
        exporter.load_queries()


def test_escape():
    assert exporter.escape('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def run(monkeypatch, rows):
    if isinstance(rows, Exception):

        def fake(*args):
            raise rows
    else:

        def fake(*args):
            return rows

    monkeypatch.setattr(exporter, "run_query", fake)
    samples, status = {}, []
    exporter.run_one(PROJECT, query(), "3600s", "start", "end", samples, status)
    return samples, {name: value for name, _, value in status}


def test_rows_become_labelled_samples(monkeypatch):
    samples, status = run(monkeypatch, [{"environment": "prod", "count_count": "3"}])
    assert samples["m"] == ("M.", ['m{project="demo",window="3600s",environment="prod"} 3.0'])
    assert status["langfuse_exporter_query_success"] == 1


@pytest.mark.parametrize("value", [None, "n/a"])
def test_null_or_non_numeric_value_skipped(monkeypatch, value):
    samples, _ = run(monkeypatch, [{"environment": "prod", "count_count": value}])
    assert samples == {}


def test_failed_query_reports_failure(monkeypatch):
    samples, status = run(monkeypatch, urllib.error.URLError("down"))
    assert samples == {}
    assert status["langfuse_exporter_query_success"] == 0


def test_collect_renders_exposition_format(monkeypatch):
    monkeypatch.setattr(exporter, "WINDOWS", [3600])
    monkeypatch.setattr(exporter, "run_query", lambda *args: [{"environment": "prod", "count_count": 2}])
    out = exporter.collect([PROJECT], [query()]).decode()
    lines = out.splitlines()
    assert "# HELP m M." in lines
    assert "# TYPE m gauge" in lines
    assert 'm{project="demo",window="3600s",environment="prod"} 2.0' in lines
    assert 'langfuse_exporter_query_success{project="demo",window="3600s",query="q"} 1' in lines
    assert out.endswith("\n")

"""Independently reproduced release-boundary regressions; no real model calls."""

import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest

from llm_ops_lab.marketing import offline_model
from llm_ops_lab.marketing.agent import baseline
from llm_ops_lab.marketing.contracts import ContractError, canonical
from llm_ops_lab.marketing.diagnostics import Scope, Snapshot
from llm_ops_lab.marketing.fixtures import scenario
from llm_ops_lab.marketing.graph import run_graph
from llm_ops_lab.marketing.pipeline import publish_dataset, read_published


@pytest.fixture
def source():
    return json.loads(Path("data/marketing/v1/acceptance.json").read_text())


@pytest.fixture(autouse=True)
def isolated_test_worker_lock(tmp_path, monkeypatch):
    # Fake workers must not contend with an independently running real inference.
    monkeypatch.setattr(offline_model.tempfile, "gettempdir", lambda: str(tmp_path))


def test_review_committed_publication_survives_cleanup_failure(
    source, tmp_path, monkeypatch, caplog,
):
    first = publish_dataset(source, tmp_path)
    original = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path.name.startswith(".ROLLBACK-"):
            raise OSError("injected rollback cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    result = publish_dataset(scenario(source, "corrected_spend"), tmp_path)
    assert result["snapshot_id"] != first["snapshot_id"]
    assert read_published(tmp_path)[0]["snapshot_id"] == result["snapshot_id"]
    assert not list((tmp_path / "quarantine").glob("*/failure.json"))
    assert "cleanup" in caplog.text.lower()


def test_review_reused_snapshot_cleanup_preserves_success(source, tmp_path, monkeypatch, caplog):
    first = publish_dataset(source, tmp_path)

    def fail_cleanup(*args, **kwargs):
        raise OSError("injected staging cleanup failure")

    monkeypatch.setattr("llm_ops_lab.marketing.pipeline.shutil.rmtree", fail_cleanup)
    assert publish_dataset(source, tmp_path)["snapshot_id"] == first["snapshot_id"]
    assert read_published(tmp_path)[0]["snapshot_id"] == first["snapshot_id"]
    assert not list((tmp_path / "quarantine").glob("*/failure.json"))
    assert "cleanup" in caplog.text.lower()


@pytest.mark.parametrize("change", ["float", "bool", "duplicate_key"])
def test_review_bronze_exact_types_and_unique_keys(source, tmp_path, change):
    raw_source = deepcopy(source)
    if change == "float":
        raw_source["spend_snapshots"][0]["spend_krw"] = 10000.0
    elif change == "bool":
        raw_source["spend_snapshots"][0]["revision"] = True
    raw = canonical(raw_source)
    if change == "duplicate_key":
        raw = raw.replace(b'"schema_version":"marketing.v1"',
                          b'"schema_version":"marketing.v1","schema_version":"marketing.v1"')
    with pytest.raises(ContractError):
        publish_dataset(source, tmp_path, raw_bytes=raw)
    assert not (tmp_path / "CURRENT.json").exists()


def test_review_large_integer_metrics_and_exact_decline(source, tmp_path):
    source["accounts"] = ["alpha"]
    source["campaigns"] = [source["campaigns"][0]]
    source["refunds"] = []
    source["coverage"] = [r for r in source["coverage"] if r["account_id"] == "alpha"]
    source["spend_snapshots"] = [r | {"spend_krw": 3} for r in source["spend_snapshots"]
                                 if r["account_id"] == "alpha" and r["campaign_id"] == "c1"]
    template = source["orders"][0]
    source["orders"] = []
    for date, total in ((source["date_start"], 2**53 + 1), (source["date_end"], 2**53)):
        whole, remainder = divmod(total, 10**12)
        amounts = [10**12] * whole + [remainder]
        source["orders"].extend(template | {
            "order_id": f"{date}-{index}", "gross_sales_krw": amount,
            "event_time": f"{date}T02:00:00Z", "ingested_at": f"{date}T02:01:00Z",
        } for index, amount in enumerate(amounts))
    publication = publish_dataset(source, tmp_path / "product")
    scope = Scope(publication["snapshot_id"], "alpha", source["date_start"], source["date_end"])
    snapshot = Snapshot.open(tmp_path / "product", scope)
    rows = read_published(tmp_path / "product")[1]["account"]
    assert rows[0]["roas"] == rows[1]["roas"]  # Display DOUBLE cannot distinguish these.
    assert baseline(snapshot, source["as_of"])["result"]["conclusion"] == "observed_decline"
    graph = run_graph(tmp_path / "product", tmp_path / "graph.sqlite", "large", scope,
                      now=source["as_of"])
    assert graph["result"]["conclusion"] == "observed_decline"


def child(command):
    return subprocess.Popen([sys.executable, "-u", "-c", command], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def reap(process):
    if process.poll() is None:
        process.kill()
    process.communicate()


def test_review_partial_frame_obeys_full_deadline():
    process = child("import sys,time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(0.6)")
    model = offline_model.OfflineModel()
    model.process = process
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            model._read(0.05)
        assert time.monotonic() - started < 0.4
        assert process.poll() is not None
    finally:
        reap(process)


def test_review_oversized_model_frame_rejected():
    process = child("import json; print(json.dumps({'output':'x'*300000}))")
    model = offline_model.OfflineModel()
    model.process = process
    try:
        with pytest.raises(ValueError, match="frame|response|output"):
            model._read(2)
    finally:
        reap(process)


def test_review_fragmented_frame_is_reassembled():
    process = child("import sys,time; sys.stdout.write('{'); sys.stdout.flush(); "
                    "time.sleep(0.02); print('\"output\":\"ok\"}')")
    model = offline_model.OfflineModel()
    model.process = process
    try:
        assert model._read(2) == {"output": "ok"}
    finally:
        reap(process)


def test_review_blocked_model_write_obeys_deadline(monkeypatch):
    original = subprocess.Popen
    owned = []

    def launch(*args, **kwargs):
        process = original([sys.executable, "-u", "-c",
                            'import time; print(\'{"identity": {}}\'); time.sleep(0.6)'],
                           **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(offline_model.subprocess, "Popen", launch)
    with offline_model.OfflineModel() as model:
        # Fill the kernel pipe first; never depend on a particular OS pipe capacity.
        fd = model.process.stdin.fileno()
        os.set_blocking(fd, False)
        try:
            while True:
                os.write(fd, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(fd, True)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            model.generate([{"role": "user", "content": "blocked"}], 0.05)
        assert time.monotonic() - started < 0.4
    assert owned[0].poll() is not None


def test_review_startup_interrupt_cleans_all_resources(monkeypatch):
    original = subprocess.Popen

    def launch(*args, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(offline_model.subprocess, "Popen", launch)
    monkeypatch.setattr(offline_model.OfflineModel, "_read", interrupt)
    model = offline_model.OfflineModel()
    try:
        with pytest.raises(KeyboardInterrupt):
            model.__enter__()
        assert model.process.poll() is not None
        assert model.lock.closed and model.stderr.closed
    finally:
        model.__exit__(None, None, None)


def test_review_spawn_failure_cleans_acquired_resources(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected spawn failure")

    monkeypatch.setattr(offline_model.subprocess, "Popen", fail)
    model = offline_model.OfflineModel()
    try:
        with pytest.raises(OSError, match="spawn failure"):
            model.__enter__()
        assert model.lock.closed and model.stderr.closed
    finally:
        model.__exit__(None, None, None)

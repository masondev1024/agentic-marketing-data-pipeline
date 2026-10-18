"""Real PostgreSQL + RDF/OWL/SHACL + HTTP; planner is explicitly a test fixture."""

import asyncio
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from test_olist_api import StubSupervisor
from test_olist_postgres import ADMIN, current
from test_olist_postgres import database as database
from test_olist_validation import _make_fixture

from llm_ops_lab.api import create_app
from llm_ops_lab.olist import semantics
from llm_ops_lab.olist.durable_graph import budget_status, investigate_durable
from llm_ops_lab.olist.pipeline import run_pipeline
from llm_ops_lab.service import ServiceConfig

pytestmark = pytest.mark.skipif(not ADMIN, reason="dedicated Olist PostgreSQL not enabled")


def test_real_database_graph_and_http_replay(tmp_path, database):
    writer, reader = database
    published = run_pipeline(tmp_path, writer, intake_path=_make_fixture(tmp_path))
    journal = tmp_path / "investigations.sqlite"
    calls = []

    def generate(*_):
        calls.append(1)
        return {"output": '{"requests":["coverage"]}', "model_id": "fixture-planner",
                "model_revision": "fixture-v1", "prompt_tokens": 4, "output_tokens": 4}

    generate.durable_identity = {"provider": "fixture", "model": "fixture-planner"}

    class DatabaseSupervisor(StubSupervisor):
        async def submit(self, request_id, settings):
            result = await asyncio.to_thread(
                investigate_durable, journal, reader, settings["question"], generate,
                principal=settings["principal"], idempotency_key=settings["idempotency_key"],
            )
            return result | {"request_id": request_id, "queue_seconds": 0,
                             "request_seconds": 0, "provider": "fixture"}

    config = ServiceConfig(output_dir=tmp_path, olist_root=tmp_path,
                           olist_engine="intent-v1", olist_provider="nvidia-super",
                           durable_db=journal)
    with TestClient(create_app(config, supervisor=DatabaseSupervisor()),
                    base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        arguments = {"json": {"question": "표본에 연결되지 않은 계약은?"},
                     "headers": {"idempotency-key": "semantic-test-001"}}
        first = client.post("/v1/investigations", **arguments)
        second = client.post("/v1/investigations", **arguments)
    assert first.status_code == second.status_code == 200, (first.json(), second.json())
    initial, replay = first.json(), second.json()
    assert initial["status"] == "completed"
    assert initial["provider"] == "fixture"
    assert initial["semantic_validation"]["dataset_version"] == published["version_id"]
    assert initial["semantic_validation"]["declared_source_paths_validated"]
    assert initial["findings"][0]["observed_counts"] == {
        "numerator": 1, "denominator": 1, "unit": "contracts",
        "scope": "published_channel_aggregate",
    }
    assert replay["replayed"] is True
    assert replay["semantic_validation"] == initial["semantic_validation"]
    assert len(calls) == budget_status(journal)["reserved"] == 1
    assert current(reader) == published["version_id"]


def test_real_database_cli_and_bad_contract_preserve_published_version(
    tmp_path, database, monkeypatch, capsys
):
    import sys

    writer, reader = database
    published = run_pipeline(tmp_path, writer, intake_path=_make_fixture(tmp_path))
    monkeypatch.setenv("OLIST_READER_DSN", reader)
    monkeypatch.setattr(sys, "argv", ["semantics", "--root", str(tmp_path)])
    assert semantics.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dataset_version"] == published["version_id"]
    shutil.copytree(semantics.ASSET_DIR, tmp_path / "changed-semantics")
    monkeypatch.setattr(semantics, "ASSET_DIR", tmp_path / "changed-semantics")
    shapes = semantics.ASSET_DIR / "shapes-v1.ttl"
    shapes.write_bytes(shapes.read_bytes() + b"\n# drift\n")
    assert semantics.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed", "code": "semantic_contract_unavailable"
    }
    assert current(reader) == published["version_id"]

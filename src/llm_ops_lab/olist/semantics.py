"""Offline, bounded RDF projection, OWL-RL reasoning and SHACL validation.

Only trusted local Turtle assets are parsed. User/model text is never RDF input.
PostgreSQL evidence remains the numeric authority; this layer validates its
versioned semantic links and does not infer missing sales outcomes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from owlrl import DeductiveClosure, OWLRL_Semantics
from pyshacl import validate
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.compare import to_canonical_graph
from rdflib.namespace import OWL, RDF, SH, XSD

from . import agent as evidence_contract
from .diagnostics import _canonical
from .lineage import _source_fingerprint

EX = Namespace("urn:olist:semantic:")
ASSET_DIR = Path(__file__).parents[3] / "data/olist/semantics"
POLICY_PATH = Path(__file__).parents[3] / "data/olist/metric-policy-v1.json"
CONTRACT_VERSION = "olist.semantics.v1"
MAX_INPUT_BYTES = 32_768
MAX_ASSERTED_TRIPLES = 400
MAX_INFERRED_TRIPLES = 4_000
TOOLS = frozenset({
    "version_evidence", "channel_funnel", "seller_funnel", "run_history", "definitions"
})
METRICS = (
    ("sample_lead_count", "lead_count", "leads"),
    ("sample_contract_count", "contract_count", "contracts"),
    ("sample_linked_contract_count", "linked_contract_count", "contracts"),
    ("sample_unlinked_contract_count", "unlinked_contract_count", "contracts"),
    ("sample_missing_origin_count", None, "leads"),
)


class SemanticValidationError(ValueError):
    """A stable public code; never exposes parser output or input literals."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Contract:
    ontology: bytes
    shapes: bytes
    policy_sha256: str
    asset_sha256: dict[str, str]
    engines: dict[str, str]
    transform_sha256: str


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def load_contract() -> Contract:
    """Fail closed on missing/edited assets; there are no remote import paths."""
    try:
        raw = (ASSET_DIR / "manifest-v1.json").read_bytes()
        manifest = json.loads(raw, object_pairs_hook=_unique_keys)
        if manifest["contract_version"] != CONTRACT_VERSION:
            raise ValueError
        contents = {name: (ASSET_DIR / name).read_bytes()
                    for name in ("ontology-v1.ttl", "shapes-v1.ttl")}
        expected = manifest["files"]
        if set(expected) != set(contents):
            raise ValueError
        if any(_hash(data) != expected[name] for name, data in contents.items()):
            raise ValueError
        policy_hash = _hash(POLICY_PATH.read_bytes())
        if policy_hash != manifest["metric_policy_sha256"]:
            raise ValueError
        engines = {name: package_version(name) for name in ("rdflib", "owlrl", "pyshacl")}
        if manifest["engines"] != engines:
            raise ValueError
        transform = _source_fingerprint()[1]
        if manifest["transform_sha256"] != transform:
            raise ValueError
        return Contract(contents["ontology-v1.ttl"], contents["shapes-v1.ttl"],
                        policy_hash, expected | {"manifest-v1.json": _hash(raw)}, engines,
                        transform)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise SemanticValidationError("semantic_contract_unavailable") from error


def _validated_bundle(bundle: Any, contract: Contract) -> tuple[str, dict[str, dict]]:
    try:
        if len(_canonical(bundle)) > MAX_INPUT_BYTES:
            raise ValueError
        if type(bundle) is not list or not 1 <= len(bundle) <= len(TOOLS):
            raise ValueError
        if bundle[0]["tool"] != "version_evidence":
            raise ValueError
        version = bundle[0]["dataset_version"]
        if evidence_contract.diagnostics._safe_version(version) is None:
            raise ValueError
        by_tool = {}
        for item in bundle:
            tool = item["tool"]
            if tool not in TOOLS or tool in by_tool:
                raise ValueError
            evidence_contract._validate_tool_result(item, tool, version)
            by_tool[tool] = item
        quality = by_tool["version_evidence"]["data"]
        if quality["quality_status"] != "passed" or quality["failed_check_count"] != 0:
            raise ValueError
        if quality["version_id"] != version:
            raise ValueError
        if quality.get("transform_sha256") != contract.transform_sha256:
            raise ValueError
        if "channel_funnel" in by_tool:
            _validate_counts(by_tool["channel_funnel"]["data"])
        definitions = by_tool.get("definitions")
        if definitions is not None:
            data = definitions["data"]
            if data["policy_sha256"] != contract.policy_sha256:
                raise ValueError
            actual = json.loads(POLICY_PATH.read_bytes(), object_pairs_hook=_unique_keys)
            if data["metric_policy"] != actual:
                raise ValueError
        return version, by_tool
    except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
        raise SemanticValidationError("semantic_evidence_invalid") from error


def _validate_counts(rows: Any) -> None:
    if type(rows) is not list or len(rows) > 64:
        raise ValueError
    origins = set()
    fields = [field for _, field, _ in METRICS if field is not None]
    for row in rows:
        if type(row) is not dict or "origin" not in row:
            raise ValueError
        origin = row["origin"]
        if origin is not None and (type(origin) is not str or len(origin) > 120):
            raise ValueError
        if origin in origins:
            raise ValueError
        origins.add(origin)
        if any(type(row.get(field)) is not int or not 0 <= row[field] <= 2**63 - 1
               for field in fields):
            raise ValueError
        if (row["linked_contract_count"] + row["unlinked_contract_count"]
                != row["contract_count"] or row["contract_count"] > row["lead_count"]):
            raise ValueError


def _local_graph(data: bytes) -> Graph:
    graph = Graph().parse(data=data, format="turtle")
    if any(graph.triples((None, OWL.imports, None))):
        raise SemanticValidationError("semantic_contract_unavailable")
    return graph


def project_bundle(bundle: list[dict], contract: Contract | None = None) -> Graph:
    contract = contract or load_contract()
    version, by_tool = _validated_bundle(bundle, contract)
    try:
        graph = _local_graph(contract.ontology)
        version_node = URIRef("urn:olist:version:" + _hash(version.encode()))
        graph.add((version_node, RDF.type, EX.DatasetVersion))
        graph.add((version_node, EX.versionId, Literal(version)))
        for source in (EX.marketing, EX.commerce):
            graph.add((version_node, EX.releaseContainsSource, source))
        for tool, item in by_tool.items():
            node = URIRef("urn:olist:evidence:" + item["evidence_id"])
            graph.add((node, RDF.type, EX.ToolEvidence))
            graph.add((node, EX.evidenceId, Literal(item["evidence_id"])))
            graph.add((node, EX.datasetVersion, Literal(item["dataset_version"])))
            graph.add((node, EX.inVersion, version_node))
            if tool == "channel_funnel" and item["data"]:
                _project_counts(graph, node, version_node, item)
        if len(graph) > MAX_ASSERTED_TRIPLES:
            raise SemanticValidationError("semantic_graph_bounds_exceeded")
        return graph
    except SemanticValidationError:
        raise
    except Exception as error:
        raise SemanticValidationError("semantic_contract_unavailable") from error


def _project_counts(graph: Graph, evidence: URIRef, version: URIRef, item: dict) -> None:
    rows = item["data"]
    for metric, field, unit in METRICS:
        value = (sum(row[field] for row in rows) if field is not None else
                 sum(row["lead_count"] for row in rows if row["origin"] is None))
        node = URIRef(f"urn:olist:observation:{item['evidence_id']}:{metric}")
        for predicate, obj in (
            (RDF.type, EX.CountObservation), (EX.forMetric, EX[metric]),
            (EX.value, Literal(value, datatype=XSD.integer)),
            (EX.unit, Literal(unit)), (EX.scope, Literal("source_sample")),
            (EX.sourceEvidence, evidence), (EX.inVersion, version),
        ):
            graph.add((node, predicate, obj))


def graph_fingerprint(graph: Graph) -> str:
    """RDF blank-node canonicalization and sorted terms avoid serializer ordering drift."""
    triples = sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in to_canonical_graph(graph))
    return _hash("\n".join(triples).encode("utf-8"))


def validate_graph(graph: Graph, contract: Contract | None = None) -> dict:
    """Expand a generated bounded graph in place, then check the trusted SHACL shapes."""
    contract = contract or load_contract()
    if len(graph) > MAX_ASSERTED_TRIPLES:
        raise SemanticValidationError("semantic_graph_bounds_exceeded")
    before = len(graph)
    try:
        DeductiveClosure(OWLRL_Semantics, axiomatic_triples=False,
                         datatype_axioms=False).expand(graph)
        if len(graph) > MAX_INFERRED_TRIPLES:
            raise SemanticValidationError("semantic_graph_bounds_exceeded")
        conforms, report, _ = validate(
            graph, shacl_graph=_local_graph(contract.shapes), inference="none",
            advanced=False, js=False, do_owl_imports=False, inplace=False,
            abort_on_first=False, allow_infos=False, allow_warnings=False,
            max_validation_depth=10,
        )
        if conforms is not True or not isinstance(report, Graph):
            raise SemanticValidationError("semantic_constraints_failed")
        if list(report.subjects(RDF.type, SH.ValidationResult)):
            raise SemanticValidationError("semantic_constraints_failed")
        return {"asserted_triples": before, "inferred_triples": len(graph) - before,
                "graph_sha256": graph_fingerprint(graph)}
    except SemanticValidationError:
        raise
    except Exception as error:
        raise SemanticValidationError("semantic_validation_unavailable") from error


def validate_bundle(bundle: list[dict]) -> dict:
    contract = load_contract()
    version, _ = _validated_bundle(bundle, contract)
    graph = project_bundle(bundle, contract)
    statistics = validate_graph(graph, contract)
    observations = []
    for node in sorted(graph.subjects(RDF.type, EX.CountObservation), key=str):
        metric = graph.value(node, EX.forMetric)
        evidence = graph.value(node, EX.sourceEvidence)
        sources = sorted(graph.objects(metric, EX.declaredSource), key=str)
        if not sources or any(source not in {EX.marketing, EX.commerce} for source in sources):
            raise SemanticValidationError("semantic_provenance_invalid")
        if not all((node, EX.derivedFrom, source) in graph for source in sources):
            raise SemanticValidationError("semantic_provenance_invalid")
        metric_name = str(metric).removeprefix(str(EX))
        entry = {"metric": metric_name, "value": int(graph.value(node, EX.value)),
                 "unit": str(graph.value(node, EX.unit)), "scope": "source_sample",
                 "evidence_id": str(graph.value(evidence, EX.evidenceId)),
                 "declared_sources": [str(source).removeprefix(str(EX)) for source in sources]}
        interpretation = graph.value(metric, EX.interpretation)
        if interpretation is not None:
            entry["interpretation"] = str(interpretation)
        observations.append(entry)
    return {"status": "passed", "contract_version": CONTRACT_VERSION,
            "dataset_version": version, "assets": contract.asset_sha256,
            "engine_versions": contract.engines,
            "transform_sha256": contract.transform_sha256,
            "metric_policy_sha256": contract.policy_sha256, **statistics,
            "validation_scope": ("count_observations_and_declared_dependencies" if observations
                                 else "evidence_structure_only"),
            "declared_source_paths_validated": bool(observations),
            "row_level_provenance_verified": False,
            "observations": observations,
            "limits": ["sample_only", "unlinked_is_unknown_not_zero",
                       "no_currency_or_time_attribution"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", type=Path,
                        help="Local JSON list of version-bound evidence; no RDF/URL input")
    source.add_argument("--root", type=Path, help="Read the project's local PostgreSQL snapshot")
    parser.add_argument("--version", help="Optional published data version for --root")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        parser.error("timeout must be finite and in (0, 60]")
    if args.bundle is not None and args.version is not None:
        parser.error("version is only valid with --root")
    deadline = time.monotonic() + args.timeout
    try:
        bundle = _cli_bundle(args, deadline)
        result = validate_bundle(bundle)
        if time.monotonic() >= deadline:
            raise SemanticValidationError("semantic_deadline_exceeded")
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        result = {"status": "failed", "code": getattr(error, "code", "semantic_evidence_invalid")}
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        _save_report(args.output, text)
    print(text, end="")
    return 0 if result["status"] == "passed" else 2


def _cli_bundle(args, deadline):
    if args.root is not None:
        from .config import connection_info
        from .tool_session import OlistToolSession

        with OlistToolSession(connection_info(args.root.resolve(), reader=True),
                              version_id=args.version, deadline=deadline) as session:
            return session.collect_bundle(["channel_funnel", "definitions"])
    with args.bundle.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise SemanticValidationError("semantic_evidence_invalid")
    return json.loads(raw, object_pairs_hook=_unique_keys)


def _save_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".semantic-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())

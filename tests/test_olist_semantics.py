"""Real OWL-RL expansion and SHACL checks over bounded, versioned evidence."""

import copy
import json

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from test_olist_agent import FakeSession

from llm_ops_lab.olist import semantics


@pytest.fixture
def bundle():
    session = FakeSession("unused", version_id="v1", deadline=1)
    return copy.deepcopy([session.initialize(), session.call("channel_funnel")])


def test_real_reasoning_provides_source_paths_and_preserves_unknown(bundle):
    graph = semantics.project_bundle(bundle)
    node = next(graph.subjects(RDF.type, semantics.EX.CountObservation))
    assert (node, RDF.type, semantics.EX.Observation) not in graph
    report = semantics.validate_bundle(bundle)
    assert report["status"] == "passed"
    assert report["dataset_version"] == "v1"
    assert report["inferred_triples"] > 0
    assert report["declared_source_paths_validated"] is True
    assert report["row_level_provenance_verified"] is False
    observations = {entry["metric"]: entry for entry in report["observations"]}
    assert observations["sample_contract_count"]["value"] == 3
    assert observations["sample_unlinked_contract_count"]["value"] == 1
    assert observations["sample_unlinked_contract_count"]["interpretation"] == "unobserved_not_zero"
    assert observations["sample_missing_origin_count"]["value"] == 3
    assert observations["sample_lead_count"]["declared_sources"] == ["marketing"]
    assert observations["sample_unlinked_contract_count"]["declared_sources"] == [
        "commerce", "marketing"
    ]
    semantics.validate_graph(graph)
    metric = graph.value(node, semantics.EX.forMetric)
    assert (node, RDF.type, semantics.EX.Observation) in graph
    assert (metric, semantics.EX.hasObservation, node) in graph
    assert (node, semantics.EX.derivedFrom, semantics.EX.marketing) in graph
    if metric == semantics.EX.sample_lead_count:
        assert (node, semantics.EX.derivedFrom, semantics.EX.commerce) not in graph
    assert not list(graph.triples((None, semantics.EX.salesOutcome, None)))
    assert not list(graph.subjects(RDF.type, semantics.EX.ZeroSalesSeller))


def test_content_hash_and_row_order_are_stable(bundle):
    first = semantics.validate_bundle(bundle)
    assert semantics.validate_bundle(copy.deepcopy(bundle))["graph_sha256"] == first["graph_sha256"]
    graph = semantics.project_bundle(bundle)
    reverse = Graph()
    for triple in reversed(list(graph)):
        reverse.add(triple)
    assert semantics.graph_fingerprint(reverse) == semantics.graph_fingerprint(graph)
    reordered = copy.deepcopy(bundle)
    reordered[1]["data"].reverse()
    from llm_ops_lab.olist.diagnostics import _evidence_id

    reordered[1]["evidence_id"] = _evidence_id("v1", "channel_funnel", reordered[1]["data"])
    # Evidence IDs commit to row order; replaying identical evidence is deterministic.
    assert semantics.validate_bundle(reordered)["status"] == "passed"
    assert semantics.validate_bundle(reordered)["graph_sha256"] != first["graph_sha256"]


def test_version_only_bundle_cannot_claim_validated_metric_lineage(bundle):
    result = semantics.validate_bundle(bundle[:1])
    assert result["status"] == "passed"
    assert result["validation_scope"] == "evidence_structure_only"
    assert result["declared_source_paths_validated"] is False
    assert result["observations"] == []


def test_source_mapping_requires_matching_published_transform(bundle):
    from llm_ops_lab.olist.diagnostics import _evidence_id

    bundle[0]["data"]["transform_sha256"] = "f" * 64
    bundle[0]["evidence_id"] = _evidence_id("v1", "version_evidence", bundle[0]["data"])
    with pytest.raises(semantics.SemanticValidationError):
        semantics.validate_bundle(bundle)


@pytest.mark.parametrize("property_name", ["sourceEvidence", "forMetric", "inVersion", "unit"])
def test_shacl_rejects_missing_required_properties(bundle, property_name):
    graph = semantics.project_bundle(bundle)
    node = next(graph.subjects(RDF.type, semantics.EX.CountObservation))
    graph.remove((node, semantics.EX[property_name], None))
    with pytest.raises(semantics.SemanticValidationError, match="semantic_constraints_failed"):
        semantics.validate_graph(graph)


@pytest.mark.parametrize("value", [
    Literal(-1), Literal("3"), Literal(True), Literal("3.0", datatype=XSD.decimal)
])
def test_shacl_rejects_negative_or_wrongly_typed_counts(bundle, value):
    graph = semantics.project_bundle(bundle)
    node = next(graph.subjects(RDF.type, semantics.EX.CountObservation))
    graph.set((node, semantics.EX.value, value))
    with pytest.raises(semantics.SemanticValidationError, match="semantic_constraints_failed"):
        semantics.validate_graph(graph)


def test_shacl_rejects_wrong_metric_unit(bundle):
    graph = semantics.project_bundle(bundle)
    node = next(graph.subjects(RDF.type, semantics.EX.CountObservation))
    graph.set((node, semantics.EX.unit, Literal("currency")))
    with pytest.raises(semantics.SemanticValidationError, match="semantic_constraints_failed"):
        semantics.validate_graph(graph)


def test_shacl_rejects_other_version_and_unknown_predicate(bundle):
    graph = semantics.project_bundle(bundle)
    evidence = next(graph.subjects(RDF.type, semantics.EX.ToolEvidence))
    graph.set((evidence, semantics.EX.datasetVersion, Literal("other")))
    with pytest.raises(semantics.SemanticValidationError):
        semantics.validate_graph(graph)
    graph = semantics.project_bundle(bundle)
    node = next(graph.subjects(RDF.type, semantics.EX.CountObservation))
    graph.add((node, semantics.EX.salesOutcome, Literal("no_sales")))
    with pytest.raises(semantics.SemanticValidationError):
        semantics.validate_graph(graph)


@pytest.mark.parametrize("mutation", ["value", "version", "id", "duplicate", "quality"])
def test_invalid_evidence_is_rejected_before_graph_projection(bundle, mutation):
    changed = copy.deepcopy(bundle)
    if mutation == "value":
        changed[1]["data"][0]["lead_count"] += 1
    elif mutation == "version":
        changed[1]["dataset_version"] = "other"
    elif mutation == "id":
        changed[1]["evidence_id"] = "0" * 64
    elif mutation == "duplicate":
        changed.append(changed[1])
    else:
        changed[0]["data"]["quality_status"] = "failed"
    with pytest.raises(semantics.SemanticValidationError):
        semantics.validate_bundle(changed)


def test_assets_must_match_the_frozen_manifest(tmp_path, monkeypatch, bundle):
    import shutil

    shutil.copytree(semantics.ASSET_DIR, tmp_path / "semantics")
    monkeypatch.setattr(semantics, "ASSET_DIR", tmp_path / "semantics")
    shape = semantics.ASSET_DIR / "shapes-v1.ttl"
    shape.write_bytes(shape.read_bytes() + b"\n# changed\n")
    with pytest.raises(semantics.SemanticValidationError, match="semantic_contract_unavailable"):
        semantics.validate_bundle(bundle)


def test_definitions_policy_is_bound_to_the_same_contract(bundle):
    session = FakeSession("unused", version_id="v1", deadline=1)
    bundle.append(session.call("definitions"))
    assert semantics.validate_bundle(bundle)["status"] == "passed"
    bundle[-1]["data"]["metric_policy"]["currency"]["value"] = "invented"
    with pytest.raises(semantics.SemanticValidationError):
        semantics.validate_bundle(bundle)


def test_unsafe_or_oversized_inputs_are_rejected(bundle):
    with pytest.raises(semantics.SemanticValidationError):
        semantics.validate_bundle([])
    changed = copy.deepcopy(bundle)
    changed[1]["data"] = "x" * 100_000
    with pytest.raises(semantics.SemanticValidationError):
        semantics.validate_bundle(changed)


def test_projection_never_interprets_evidence_text_as_rdf_or_imports(bundle):
    from llm_ops_lab.olist.diagnostics import _evidence_id

    bundle[1]["data"][1]["origin"] = 'http://127.0.0.1:9/secret'
    bundle[1]["evidence_id"] = _evidence_id("v1", "channel_funnel", bundle[1]["data"])
    graph = semantics.project_bundle(bundle)
    assert not list(graph.triples((URIRef('http://127.0.0.1:9/secret'), None, None)))
    assert semantics.validate_bundle(bundle)["status"] == "passed"
    assert '127.0.0.1:9' not in json.dumps(semantics.validate_bundle(bundle))


@pytest.mark.parametrize("value", [True, -1, 1.0, "1"])
def test_signed_but_bad_count_types_are_not_coerced(bundle, value):
    from llm_ops_lab.olist.diagnostics import _evidence_id

    bundle[1]["data"][0]["lead_count"] = value
    bundle[1]["evidence_id"] = _evidence_id("v1", "channel_funnel", bundle[1]["data"])
    with pytest.raises(semantics.SemanticValidationError):
        semantics.validate_bundle(bundle)


def test_cli_validates_saved_evidence_and_fails_closed(tmp_path, monkeypatch, capsys, bundle):
    import sys

    source, output = tmp_path / "bundle.json", tmp_path / "report.json"
    source.write_text(json.dumps(bundle))
    monkeypatch.setattr(sys, "argv", ["semantics", "--bundle", str(source),
                                     "--output", str(output)])
    assert semantics.main() == 0
    assert json.loads(output.read_text())["status"] == "passed"
    source.write_text('[{"tool":"first","tool":"duplicate"}]')
    assert semantics.main() == 2
    assert json.loads(output.read_text())["status"] == "failed"
    assert "duplicate_json_key" not in capsys.readouterr().out

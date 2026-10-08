"""An explainable lexical baseline for a small, trusted local corpus."""

import hashlib
import json
import math
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from llm_ops_lab.context import ContextDocument, load_document
from llm_ops_lab.query_support import (
    POLICY_VERSION,
    check_word_support,
    normalized_words,
    unsupported_characters,
)

MAX_DOCUMENTS = 32
MIN_COVERAGE = 0.5
AMBIGUITY_RATIO = 0.95
RETRIEVER_VERSION = "weighted-lexical.v1"
TOKENIZER_VERSION = "hangul-bigram-latin-word.v1"
RETRIEVERS = ("baseline", "scoped-v2", "scoped-v3")
SCOPE_FILENAME = "scope.json"
TOKEN_PATTERN = re.compile(r"[가-힣]+|[a-z0-9_]+")
PARTICLES = (
    "에서는", "에서", "으로", "에게", "에는", "은", "는", "이", "가", "을", "를", "의", "에",
)


def strip_particle(word: str) -> str:
    for ending in PARTICLES:
        if word.endswith(ending) and len(word) - len(ending) >= 2:
            return word[:-len(ending)]
    return word


STOP_TEXT = (
    "데이터 파이프라인에서 필요한 이유 설명해주세요 설명해 주세요 문장으로 "
    "문서에서 대해서 어떻게 무엇인가요 알려줘 알려주세요 할까요 하나요 정보"
)
STOP_BIGRAMS = {
    word[index:index + 2]
    for word in map(strip_particle, STOP_TEXT.split())
    for index in range(len(word) - 1)
}
STOP_WORDS = {"the", "a", "an", "of", "for", "to", "in", "is", "and", "what", "why",
              "how", "please", "explain", "data", "pipeline"}


def lexical_terms(text: str) -> set[str]:
    """Korean pairs tolerate particles; this is not morphological or semantic analysis."""
    terms = set()
    for word in TOKEN_PATTERN.findall(unicodedata.normalize("NFKC", text).casefold()):
        if "가" <= word[0] <= "힣":
            word = strip_particle(word)
            terms.update(
                pair for index in range(len(word) - 1)
                if (pair := word[index:index + 2]) not in STOP_BIGRAMS
            )
        elif word not in STOP_WORDS:
            terms.add(word)
    return terms


@dataclass(frozen=True)
class SearchResult:
    document: ContextDocument | None
    metadata: dict


def load_corpus(directory: Path) -> list[ContextDocument]:
    if not directory.is_dir():
        raise ValueError(f"Corpus directory does not exist or is not a directory: {directory}")
    paths = []
    for path in directory.iterdir():
        if path.suffix.lower() in {".md", ".txt"}:
            paths.append(path)
            if len(paths) > MAX_DOCUMENTS:
                raise ValueError(f"Corpus exceeds the limit of {MAX_DOCUMENTS} documents")
    # Read and validate all files before ranking; never silently skip broken documents.
    return [load_document(path) for path in sorted(paths)]


def load_scope_contract(
    directory: Path, documents: list[ContextDocument], *, required: bool = False,
) -> ContextDocument | None:
    path = directory / SCOPE_FILENAME
    if not path.exists():
        if required:
            raise ValueError(f"Scoped retrieval requires {path}")
        return None
    contract = load_document(path)
    value = json.loads(contract.text)
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "evidence_kind", "documents",
    }:
        raise ValueError("Invalid corpus scope contract fields")
    if value["schema_version"] != "corpus-scope.v1":
        raise ValueError("Unsupported corpus scope schema")
    # This lab has no authenticated operational records or entity/time matching yet.
    if value["evidence_kind"] != "synthetic_reference":
        raise ValueError("Only synthetic_reference evidence is supported in this lab")
    actual = {Path(doc.source).name: doc.sha256 for doc in documents}
    if value["documents"] != actual:
        raise ValueError("Scope contract document hashes are stale or incomplete")
    return contract


def analyze_query(prompt: str) -> dict:
    """Bounded Korean/English heuristics, not an intent or authorization guarantee."""
    normalized = unicodedata.normalize("NFKC", prompt).casefold()
    owner = re.search(
        r"우리\s*(회사|팀)|당사|사내|운영\s*환경|프로덕션|our\s+(team|company)", normalized,
    )
    when = re.search(
        r"어제|오늘|지난|최근|이번|현재|\d{4}[-/]\d{1,2}[-/]\d{1,2}|last\s+run", normalized,
    )
    instance = re.search(r"(?:run|job|실행)[-_ ]?id\s*[:=]\s*[\w-]+", normalized)
    field = re.search(
        r"실행\s*(id|아이디|식별자)|출력\s*(위치|경로)|(?:장애|오류).*원인|"
        r"처리\s*건수|최대.*횟수|재시도.*횟수|실제.*기록|run\s*id|output\s*path", normalized,
    )
    tutorial = re.search(
        r"왜|이유|원리|개념|방법|어떻게|절차|설계|기록해야|"
        r"(?:^|\s)(?:정의|뜻)(?:[은는이가을를]|\s|[?]|$)", normalized,
    )
    factual = re.search(r"실제|실측|측정된|발생한|설정된", normalized)
    requires_records = bool(
        field and (owner or when or instance)
        and (not tutorial or factual or when or instance)
    )
    # Remove question phrasing only from ranking, never from the model input.
    effective = re.sub(r"정확히|정확한", " ", normalized)
    effective = re.sub(
        r"몇\s*(?:초|분|시간|회|번)(?:인가요|인가|인지|입니까|일까요|일까|야)?", " ", effective,
    )
    return {
        "policy_version": "query-scope.v2",
        "required_evidence": "operational_record" if requires_records else "reference",
        "scope_signals": {
            name: match.group(0) if match else None
            for name, match in (("owner", owner), ("time", when), ("instance", instance),
                                ("field", field), ("tutorial", tutorial), ("factual", factual))
        },
        "ranking_query": effective,
        "original_query_terms": sorted(lexical_terms(prompt)),
    }


def search_documents(
    prompt: str, directory: Path, variant: str = "baseline",
) -> SearchResult:
    if variant not in RETRIEVERS:
        raise ValueError(f"Unknown retriever: {variant}")
    started = time.perf_counter()
    documents = load_corpus(directory)
    contract = load_scope_contract(directory, documents, required=variant != "baseline")
    manifest = [{"name": Path(doc.source).name, "sha256": doc.sha256} for doc in documents]
    query = analyze_query(prompt) if variant != "baseline" else None
    query_words = None
    document_words = None
    if variant == "scoped-v3":
        query_words = normalized_words(query["ranking_query"], query=True)
        document_words = [normalized_words(doc.text) for doc in documents]
        query["support_policy_version"] = POLICY_VERSION
        query["unsupported_characters"] = unsupported_characters(prompt)
        query["content_words"] = query_words
        query["ranking_query"] = " ".join(query_words)
    query_terms = lexical_terms(query["ranking_query"] if query else prompt)
    document_terms = (
        [lexical_terms(" ".join(words)) for words in document_words]
        if document_words is not None else [lexical_terms(doc.text) for doc in documents]
    )
    frequency = Counter(term for terms in document_terms for term in terms)
    weights = {
        term: 1 + math.log((len(documents) + 1) / (frequency[term] + 1))
        for term in query_terms
    }
    denominator = sum(weights.values())
    ranked = []
    for index, (document, terms) in enumerate(zip(documents, document_terms, strict=True)):
        matched = query_terms & terms
        title_terms = lexical_terms(document.text.splitlines()[0])
        if variant == "scoped-v3":
            title_terms = lexical_terms(" ".join(normalized_words(document.text.splitlines()[0])))
        coverage = sum(weights[term] for term in matched) / denominator if denominator else 0.0
        title_coverage = (
            sum(weights[term] for term in query_terms & title_terms) / denominator
            if denominator else 0.0
        )
        score = 0.8 * coverage + 0.2 * title_coverage
        candidate = {
            **document.metadata(),
            "score": score,
            "coverage": coverage,
            "matched_terms": sorted(matched),
        }
        if query_words is not None:
            checks = check_word_support(query_words, document_words[index])
            candidate["word_support"] = checks
            candidate["unsupported_words"] = [c["word"] for c in checks if not c["supported"]]
        ranked.append((document, candidate))
    ranked.sort(key=lambda item: (-item[1]["score"], item[0].source))
    selected = None
    if not documents:
        reason = "empty_corpus"
    elif query and query["required_evidence"] == "operational_record":
        reason = "unsupported_evidence_scope"
    elif query and query.get("unsupported_characters"):
        reason = "unsupported_query_characters"
    elif not query_terms:
        reason = "no_query_terms"
    elif (ranked[0][1]["coverage"] < MIN_COVERAGE
          or len(ranked[0][1]["matched_terms"]) < min(2, len(query_terms))):
        reason = "low_overlap"
    elif (len(ranked) > 1
          and ranked[1][1]["score"] >= ranked[0][1]["score"] * AMBIGUITY_RATIO):
        reason = "ambiguous"
    elif ranked[0][1].get("unsupported_words"):
        reason = "unsupported_query_terms"
    else:
        reason = "selected"
        selected = ranked[0][0]
    metadata = {
        "retriever_version": {
            "baseline": RETRIEVER_VERSION, "scoped-v2": "scoped-lexical.v2",
            "scoped-v3": "scoped-lexical.v3",
        }[variant],
        "retriever_variant": variant,
        "scope_sha256": contract.sha256 if contract else None,
        "evidence_kind": "synthetic_reference" if contract else None,
        "query_analysis": query,
        "tokenizer_version": TOKENIZER_VERSION,
        "corpus_dir": directory.as_posix(),
        "corpus_manifest": manifest,
        "corpus_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "query_terms": sorted(query_terms),
        "min_coverage": MIN_COVERAGE,
        "ambiguity_ratio": AMBIGUITY_RATIO,
        "reason": reason,
        "candidates": [metadata for _, metadata in ranked[:3]],
        "selected_source": selected.source if selected is not None else None,
        "search_seconds": time.perf_counter() - started,
    }
    return SearchResult(selected, metadata)

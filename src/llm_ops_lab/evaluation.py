"""Run a fixed development set; automate retrieval checks, leave semantics to reviewers."""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from llm_ops_lab.cli import (
    INFERENCE_SOURCE_NAMES,
    MODEL_SPECS,
    output_token_limit,
    temperature_value,
)
from llm_ops_lab.context import SYSTEM_MESSAGES
from llm_ops_lab.evidence import ANSWER_MODES
from llm_ops_lab.retrieval import RETRIEVERS, SCOPE_FILENAME, load_corpus, load_scope_contract

LOG = logging.getLogger(__name__)
MAX_DATASET_BYTES = 256 * 1024
MAX_CASES = 100
SOURCE_NAMES = (*INFERENCE_SOURCE_NAMES, "evaluation.py")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def load_cases(path: Path) -> tuple[bytes, list[dict]]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_DATASET_BYTES + 1)
    if len(raw) > MAX_DATASET_BYTES:
        raise ValueError("Dataset exceeds 256 KiB")
    cases = []
    seen = set()
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict):
            raise ValueError(f"Line {number}: expected an object")
        if set(case) != {"id", "question", "expected_source", "must_include", "must_not_claim"}:
            raise ValueError(f"Line {number}: invalid evaluation fields")
        case_id = case["id"]
        if not isinstance(case_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", case_id):
            raise ValueError(f"Line {number}: invalid id")
        if case_id in seen:
            raise ValueError(f"Duplicate case id: {case_id}")
        question = case["question"]
        if not isinstance(question, str) or not question.strip() or len(question) > 4096:
            raise ValueError(f"{case_id}: question must contain 1 to 4096 characters")
        source = case["expected_source"]
        if source is not None and (
            not isinstance(source, str) or not source or Path(source).name != source
            or source in {".", ".."}
        ):
            raise ValueError(f"{case_id}: expected_source must be a corpus filename or null")
        for field in ("must_include", "must_not_claim"):
            values = case[field]
            if not isinstance(values, list) or not values or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"{case_id}: {field} must contain nonblank review criteria")
        seen.add(case_id)
        cases.append(case)
        if len(cases) > MAX_CASES:
            raise ValueError(f"Dataset exceeds {MAX_CASES} cases")
    if not cases:
        raise ValueError("Dataset cannot be empty")
    return raw, cases


def assess_record(case: dict, record: dict, search_only: bool) -> dict:
    selected = record["retrieval"]["selected_source"]
    selected = Path(selected).name if selected else None
    if search_only:
        expected_status = "search_only"
    else:
        expected_status = "generated" if case["expected_source"] else "abstained"
    return {
        "selected_source": selected,
        "retrieval_pass": selected == case["expected_source"],
        "behavior_pass": (
            record["status"] == expected_status
            and record["model_executed"] == (expected_status == "generated")
        ),
        # Keywords cannot establish truth, relations, units, or absence of hallucination.
        "answer_quality": "not_evaluated" if search_only else "unreviewed",
        "answer_mode": record.get("answer_mode", "direct"),
        "stage_statuses": {s["name"]: s["status"] for s in record.get("stages", [])},
        "quote_membership": (record.get("evidence") or {}).get("quote_membership"),
        "selected_sentence_ids": (record.get("evidence") or {}).get("selected_ids", []),
    }


def execute_case(case: dict, args: argparse.Namespace, batch: Path, manifest: dict) -> dict:
    case_dir = batch / "cases" / case["id"]
    case_dir.mkdir(parents=True)
    command = [
        sys.executable, "-c", "from llm_ops_lab.cli import main; raise SystemExit(main())",
        "--prompt", case["question"], "--corpus-dir", "corpus",
        "--output-dir", str(case_dir), "--operator", args.operator,
        "--temperature", str(args.temperature), "--max-tokens", str(args.max_tokens),
        "--seed", str(args.seed),
        "--prompt-variant", args.prompt_variant,
        "--model", args.model,
        "--retriever", args.retriever,
        "--answer-mode", args.answer_mode,
    ]
    if args.offline:
        command.append("--offline")
    if args.search_only:
        command.append("--search-only")
    environment = os.environ.copy()
    # Stable corpus paths in model input; use the original project model cache.
    environment.setdefault("HF_HOME", str(Path("work/huggingface").resolve()))
    started = time.perf_counter()
    row = {"case_id": case["id"], "expected_source": case["expected_source"]}
    try:
        with (case_dir / "stdout.log").open("x") as stdout, (
            case_dir / "stderr.log"
        ).open("x") as stderr:
            process = subprocess.run(
                command, cwd=batch, env=environment, stdout=stdout, stderr=stderr,
                timeout=args.timeout_seconds, check=False,
            )
        paths = list(case_dir.glob("*.json"))
        if len(paths) != 1:
            if process.returncode:
                raise RuntimeError(f"CLI exit {process.returncode}; inspect stderr.log")
            raise ValueError("Expected exactly one inference record")
        record = json.loads(paths[0].read_text(encoding="utf-8"))
        if record["input"] != case["question"] or record["settings"] != manifest["settings"]:
            raise ValueError("Inference input/settings differ from the manifest")
        if record["prompt_variant"] != manifest["prompt_variant"]:
            raise ValueError("Inference prompt variant differs from the manifest")
        if record["answer_mode"] != manifest["answer_mode"]:
            raise ValueError("Inference answer mode differs from the manifest")
        if record["model_profile"] != manifest["model_profile"]:
            raise ValueError("Inference model profile differs from the manifest")
        if record["retrieval"]["corpus_sha256"] != manifest["corpus_sha256"]:
            raise ValueError("Corpus changed during evaluation")
        if record["retrieval"]["scope_sha256"] != manifest["scope_sha256"]:
            raise ValueError("Scope contract changed during evaluation")
        if record["retrieval"]["retriever_variant"] != manifest["retriever_variant"]:
            raise ValueError("Retriever variant differs from the manifest")
        expected_hashes = {
            name: manifest["source_sha256"][name] for name in INFERENCE_SOURCE_NAMES
        }
        if record["source_sha256"] != expected_hashes:
            raise ValueError("Inference code changed during evaluation")
        if record["model_executed"] and (
            record["model_id"] != manifest["model_id"]
            or record["model_revision"] != manifest["model_revision"]
        ):
            raise ValueError("Model changed during evaluation")
        if process.returncode:
            row.update(
                run_file=str(paths[0].relative_to(batch)), inference_status=record["status"],
                stage_statuses={s["name"]: s["status"] for s in record.get("stages", [])},
                pipeline_error=record.get("pipeline_error"),
                failure_diagnostics={
                    name: record.get(name) for name in (
                        "retrieval", "prompt_tokens", "generation_tokens", "generation_seconds",
                        "model_executed", "stage_journal",
                    )
                },
            )
            raise RuntimeError(f"CLI exit {process.returncode}; saved failure trace retained")
        row.update(
            execution_status="ok", run_file=str(paths[0].relative_to(batch)),
            output=record["output"], inference_status=record["status"],
            max_tokens_reached=record.get("max_tokens_reached"),
            **assess_record(case, record, args.search_only),
        )
    except (
        OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired,
    ) as error:
        row.update(execution_status="error", error=f"{type(error).__name__}: {error}")
        row["stage_journals"] = [
            str(path.relative_to(batch)) for path in sorted(case_dir.glob("*.stages.jsonl"))
        ]
    row["wall_seconds"] = time.perf_counter() - started
    write_json(case_dir / "result.json", row)
    return row


def summarize(rows: list[dict]) -> dict:
    ok = [row for row in rows if row["execution_status"] == "ok"]
    positives = [row for row in rows if row["expected_source"] is not None]
    negatives = [row for row in rows if row["expected_source"] is None]
    return {
        "total": len(rows), "executed_ok": len(ok), "errors": len(rows) - len(ok),
        "retrieval_pass": sum(row.get("retrieval_pass", False) for row in rows),
        "behavior_pass": sum(row.get("behavior_pass", False) for row in rows),
        "answerable": {
            "total": len(positives),
            "retrieval_pass": sum(row.get("retrieval_pass", False) for row in positives),
        },
        "out_of_scope": {
            "total": len(negatives),
            "retrieval_pass": sum(row.get("retrieval_pass", False) for row in negatives),
        },
        "answer_quality": "not_scored; review each answer against the rubric",
        "evidence_rejected": sum(r.get("inference_status") == "evidence_rejected" for r in rows),
        "quote_membership_verified": sum(r.get("quote_membership") == "verified" for r in rows),
    }


def write_review(batch: Path, cases: list[dict], rows: list[dict]) -> None:
    lines = [
        "# 수동 답변 평가", "",
        "자동 검색 통과는 답변 정확도 점수가 아닙니다. 각 항목을 pass/fail로 판단하고",
        "답변에서 근거를 인용하세요. 실행 오류·정답 문서 검색 실패도 삭제하지 마세요.",
        "사실/필수 내용, 근거 충실성, 두 문장 형식을 각각 채점하세요.",
        "범위 밖 질문은 적절한 보류 여부를 채점하고 두 문장 형식은 N/A로 둡니다.",
        "search-only 실행의 답변 품질은 N/A입니다. 미검토는 통과가 아닙니다.", "",
    ]
    for case, row in zip(cases, rows, strict=True):
        lines.extend([
            f"## {case['id']}", "", case["question"], "",
            f"정답 문서: {case['expected_source'] or '없음 — 보류'}",
            f"실행: {row['execution_status']}; 검색 통과: {row.get('retrieval_pass', 'N/A')}",
            f"근거 단계: {row.get('stage_statuses', {})}; "
            f"인용 존재 검증: {row.get('quote_membership') or 'N/A'} (의미 정확성 아님)",
            "", "필수 내용:", *[f"- {text}" for text in case["must_include"]],
            "", "금지 주장:", *[f"- {text}" for text in case["must_not_claim"]],
            "", "관측 출력:", "",
            *[f"> {line}" for line in row.get("output", row.get("error", "")).splitlines()],
            "", "사실/필수 내용: 미검토", "근거 충실성: 미검토", "형식: 미검토",
            "판정 근거: ", "",
        ])
    with (batch / "review.md").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines))


def run_batch(args: argparse.Namespace) -> Path:
    raw, cases = load_cases(args.dataset)
    documents = load_corpus(args.corpus_dir)
    contract = load_scope_contract(
        args.corpus_dir, documents, required=args.retriever != "baseline",
    )
    names = {Path(doc.source).name for doc in documents}
    for case in cases:
        if case["expected_source"] is not None and case["expected_source"] not in names:
            raise ValueError(f"{case['id']}: expected source absent from corpus")
    batch = (args.output_dir / str(uuid4())).resolve()
    (batch / "corpus").mkdir(parents=True)
    for doc in documents:
        with (batch / "corpus" / Path(doc.source).name).open("xb") as stream:
            stream.write(doc.text.encode("utf-8"))
    if contract:
        with (batch / "corpus" / SCOPE_FILENAME).open("xb") as stream:
            stream.write(contract.text.encode("utf-8"))
    (batch / "dataset.jsonl").write_bytes(raw)
    corpus_manifest = [{"name": Path(doc.source).name, "sha256": doc.sha256} for doc in documents]
    manifest = {
        "schema_version": "development-evaluation.v1", "batch_id": batch.name,
        "created_at": datetime.now(UTC).isoformat(), "operator": args.operator,
        "dataset_sha256": digest(raw), "dataset_kind": "development; not held-out",
        "case_count": len(cases), "search_only": args.search_only, "offline": args.offline,
        "retriever_variant": args.retriever,
        "scope_sha256": contract.sha256 if contract else None,
        "prompt_variant": args.prompt_variant,
        "answer_mode": args.answer_mode,
        "timeout_seconds": args.timeout_seconds,
        "settings": {"temperature": args.temperature, "max_tokens": args.max_tokens,
                     "seed": args.seed, "enable_thinking": False},
        "model_profile": args.model,
        "model_id": MODEL_SPECS[args.model]["id"],
        "model_revision": MODEL_SPECS[args.model]["revision"],
        "source_sha256": {
            name: digest(Path(__file__).with_name(name).read_bytes()) for name in SOURCE_NAMES
        },
        "uv_lock_sha256": (
            digest(Path("uv.lock").read_bytes()) if Path("uv.lock").is_file() else None
        ),
        "corpus_manifest": corpus_manifest,
        "corpus_sha256": digest(
            json.dumps(corpus_manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ),
    }
    write_json(batch / "manifest.json", manifest)
    LOG.info("Batch: %s", batch)
    rows = []
    for index, case in enumerate(cases, 1):
        row = execute_case(case, args, batch, manifest)
        rows.append(row)
        LOG.info("[%d/%d] %s: %s | retrieval=%s", index, len(cases), case["id"],
                 row["execution_status"], row.get("retrieval_pass", "error"))
    summary = summarize(rows)
    write_review(batch, cases, rows)
    write_json(batch / "summary.json", {
        "status": "completed_with_errors" if summary["errors"] else "completed",
        "batch_id": batch.name, "metrics": summary, "cases": rows,
    })
    return batch


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/eval/development-v1.jsonl"))
    parser.add_argument("--corpus-dir", type=Path, default=Path("data/runbooks"))
    parser.add_argument("--output-dir", type=Path, default=Path("runs/evaluations"))
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--operator", choices=["learner", "assistant"], default="learner")
    parser.add_argument("--temperature", type=temperature_value, default=0.0)
    parser.add_argument("--max-tokens", type=output_token_limit, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--prompt-variant", choices=SYSTEM_MESSAGES, default="baseline")
    parser.add_argument("--answer-mode", choices=ANSWER_MODES, default="direct")
    parser.add_argument("--model", choices=MODEL_SPECS, default="qwen3-0.6b")
    parser.add_argument("--retriever", choices=RETRIEVERS, default="baseline")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout_seconds <= 600:
        parser.error("timeout-seconds must be between 1 and 600")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        batch = run_batch(args)
        summary = json.loads((batch / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError) as error:
        LOG.error("Evaluation failed: %s", error)
        return 1
    metrics = summary["metrics"]
    print(f"검색 선택/보류 일치: {metrics['retrieval_pass']}/{metrics['total']}")
    print(f"실행 오류: {metrics['errors']} | 답변 품질: 수동 검토 필요")
    print(f"결과: {batch / 'summary.json'}\n채점표: {batch / 'review.md'}")
    return 1 if metrics["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run one bounded local generation and save its experimental provenance."""

import argparse
import hashlib
import json
import logging
import math
import os
import platform
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from llm_ops_lab.context import PROMPT_VERSIONS, SYSTEM_MESSAGES, build_messages, load_document
from llm_ops_lab.evidence import ANSWER_MODES, run_pipeline, snapshot
from llm_ops_lab.retrieval import RETRIEVERS, search_documents

MODEL_ID = "mlx-community/Qwen3-0.6B-4bit"
MODEL_REVISION = "73e3e38d981303bc594367cd910ea6eb48349da8"
MODEL_SPECS = {
    "qwen3-0.6b": {"id": MODEL_ID, "revision": MODEL_REVISION},
    "qwen3-4b": {
        "id": "mlx-community/Qwen3-4B-4bit",
        "revision": "4dcb3d101c2a062e5c1d4bb173588c54ea6c4d25",
    },
}
MAX_INPUT_TOKENS = 2048
LOG = logging.getLogger(__name__)


def temperature_value(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 2:
        raise argparse.ArgumentTypeError("temperature must be finite and between 0 and 2")
    return number


def output_token_limit(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 512:
        raise argparse.ArgumentTypeError("max-tokens must be between 1 and 512")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True, help="Use public or synthetic text only")
    context_group = parser.add_mutually_exclusive_group()
    context_group.add_argument(
        "--context-file", type=Path,
        help="Optional UTF-8 reference document, at most 16 KiB; recorded in run messages",
    )
    context_group.add_argument(
        "--corpus-dir", type=Path, help="Search up to 32 top-level Markdown/text documents",
    )
    parser.add_argument(
        "--search-only", action="store_true", help="Inspect retrieval without an LLM",
    )
    parser.add_argument("--temperature", type=temperature_value, default=0.0)
    parser.add_argument("--max-tokens", type=output_token_limit, default=128)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--prompt-variant", choices=SYSTEM_MESSAGES, default="baseline")
    parser.add_argument("--answer-mode", choices=ANSWER_MODES, default="direct")
    parser.add_argument("--model", choices=MODEL_SPECS, default="qwen3-0.6b")
    parser.add_argument("--retriever", choices=RETRIEVERS, default="baseline")
    parser.add_argument("--operator", choices=["learner", "assistant"], default="learner")
    parser.add_argument("--offline", action="store_true", help="Use cached model files only")
    parser.add_argument("--output-dir", type=Path, default=Path("runs"))
    args = parser.parse_args(argv)
    if not args.prompt.strip() or len(args.prompt) > 4096:
        parser.error("prompt must contain 1 to 4096 characters and cannot be blank")
    if args.search_only and args.corpus_dir is None:
        parser.error("--search-only requires --corpus-dir")
    if args.retriever != "baseline" and args.corpus_dir is None:
        parser.error(f"--retriever {args.retriever} requires --corpus-dir")
    if args.answer_mode != "direct" and not (args.context_file or args.corpus_dir):
        parser.error("--answer-mode evidence-v1 requires --context-file or --corpus-dir")
    return args


INFERENCE_SOURCE_NAMES = ("cli.py", "context.py", "retrieval.py", "query_support.py", "evidence.py",
                          "inference.py")


def save_result(result: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{result['run_id']}.json"
    # Exclusive creation preserves earlier experiments even if a run id is reused.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return path


def append_stage_event(path: Path, event: dict, *, start: bool = False) -> None:
    """One durable event per completed stage; final run JSON remains exclusive/immutable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if start else "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def prepare_experiment(args: argparse.Namespace) -> dict:
    # Reject bad documents before model imports, downloads, or GPU allocation.
    document = load_document(args.context_file) if args.context_file is not None else None
    retrieval = None
    status = "prepared"
    output = ""
    if args.corpus_dir is not None:
        search = search_documents(args.prompt, args.corpus_dir, args.retriever)
        document = search.document
        retrieval = search.metadata
        if args.search_only:
            status = "search_only"
            lines = [f"검색 판정: {retrieval['reason']}"]
            lines.extend(
                f"{item['source']} | score={item['score']:.3f} | "
                f"matched={','.join(item['matched_terms'])}"
                for item in retrieval["candidates"]
            )
            output = "\n".join(lines)
            if retrieval["candidates"] and "unsupported_words" in retrieval["candidates"][0]:
                unsupported = retrieval["candidates"][0]["unsupported_words"]
                output += f"\n1위 문서에서 확인하지 못한 표현: {', '.join(unsupported) or '없음'}"
        elif document is None:
            status = "abstained"
            output = "관련 문서를 명확히 선택하지 못해 답변을 보류합니다. 질문을 구체화해 주세요."
            if retrieval["reason"] == "unsupported_evidence_scope":
                output = (
                    "현재 문서는 학습용 참고 자료라 실제 회사·실행 기록을 확인할 수 없습니다. "
                    "공개 또는 비식별화한 실행 기록이 필요합니다."
                )
            elif retrieval["reason"] == "unsupported_query_terms":
                output = (
                    "관련 문서는 있지만 질문의 일부 표현을 해당 문서에서 확인하지 못해 "
                    "답변을 보류합니다. 질문을 나누거나 표현을 구체화해 주세요."
                )
            elif retrieval["reason"] == "unsupported_query_characters":
                output = (
                    "현재 검색기가 처리하지 못하는 문자가 있어 답변을 보류합니다. "
                    "질문을 한글 또는 영문으로 표현해 주세요."
                )
    evidence = None
    if args.answer_mode == "evidence-v1" and status == "prepared":
        evidence = snapshot(document)
        if not evidence["catalog"]:
            status = "abstained"
            output = "문서에 선택할 근거 문장이 없어 답변을 보류합니다."
    source_hashes = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in INFERENCE_SOURCE_NAMES
    }
    return {
        "schema_version": "inference-experiment.v2",
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "operator": args.operator,
        "status": status,
        "model_executed": False,
        "model_id": None,
        "model_revision": None,
        "model_profile": args.model,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "runner_sha256": source_hashes["cli.py"],
        "source_sha256": source_hashes,
        "settings": {
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "seed": args.seed,
            "enable_thinking": False,
        },
        "input": args.prompt,
        "answer_mode": args.answer_mode,
        "evidence": evidence,
        "messages": (
            build_messages(args.prompt, document, args.prompt_variant)
            if status == "prepared" and args.answer_mode == "direct" else []
        ),
        "sources": [document.metadata()] if document is not None and status == "prepared" else [],
        "retrieval": retrieval,
        "prompt_variant": args.prompt_variant,
        "prompt_template_version": (
            "evidence.v1" if args.answer_mode == "evidence-v1" else
            ("document" if document is not None else "manual")
            + "." + PROMPT_VERSIONS[args.prompt_variant]
        ),
        "output": output,
    }


def run_experiment(args: argparse.Namespace, runtime=None) -> dict:
    result = prepare_experiment(args)
    if result["status"] in {"search_only", "abstained"}:
        return result
    from llm_ops_lab.inference import InferenceRuntime

    runtime = runtime or InferenceRuntime(MODEL_SPECS[args.model], offline=args.offline)
    return generate_experiment(args, result, runtime)


def generate_experiment(args: argparse.Namespace, result: dict, runtime) -> dict:
    if runtime.model_spec != MODEL_SPECS[args.model]:
        raise ValueError("Runtime model does not match the requested model")

    def generate(messages, limit, trace):
        return runtime.generate(messages, limit, trace, seed=args.seed,
                                temperature=args.temperature)

    identity = dict(runtime.identity)
    if args.answer_mode == "evidence-v1":
        result.update(identity)
        journal = args.output_dir / f"{result['run_id']}.stages.jsonl"
        result["stage_journal"] = journal.name
        append_stage_event(journal, {"event": "start", "run": result}, start=True)
        run_pipeline(
            result, generate, args.max_tokens,
            checkpoint=lambda stage: append_stage_event(
                journal, {"event": "stage", "stage": stage},
            ),
        )
        stages = result["stages"]
        result.update(
            model_executed=any(stage.get("model_executed", False) for stage in stages),
            prompt_tokens=sum(stage.get("prompt_tokens", 0) for stage in stages),
            generation_tokens=sum(stage.get("generation_tokens", 0) for stage in stages),
            generation_seconds=sum(stage.get("generation_seconds", 0) for stage in stages),
            max_tokens_reached=any(stage.get("max_tokens_reached", False) for stage in stages),
            mlx_peak_memory_gb=runtime.mx.get_peak_memory() / 1e9,
        )
    else:
        result.update(identity)
        trace = {}
        try:
            generate(result["messages"], args.max_tokens, trace)
        finally:
            result.update(trace)
        result["status"] = "generated"
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        result = run_experiment(args)
        path = save_result(result, args.output_dir)
    except (ImportError, OSError, ValueError, RuntimeError) as error:
        LOG.error("Experiment failed: %s", error)
        return 1
    print(result["output"])
    for citation in result.get("citations", []):
        print(f"[{citation['id']}] {citation['source']}: {citation['text']}")
    if result.get("stages"):
        LOG.info("Evidence stages: %s | quote_membership=%s | semantics=unreviewed",
                 ", ".join(f"{stage['name']}={stage['status']}" for stage in result["stages"]),
                 result["evidence"]["quote_membership"])
    if result["retrieval"] is not None:
        LOG.info(
            "Retrieval: %s | selected=%s",
            result["retrieval"]["reason"], result["retrieval"]["selected_source"],
        )
    if not result["model_executed"]:
        LOG.info("Saved %s | status=%s | model_executed=false", path, result["status"])
        return 1 if result["status"] == "pipeline_error" else 0
    LOG.info(
        "Saved %s | prompt=%d output=%d tokens | generation=%.2fs | peak MLX=%.2fGB",
        path,
        result["prompt_tokens"],
        result["generation_tokens"],
        result["generation_seconds"],
        result["mlx_peak_memory_gb"],
    )
    if result["max_tokens_reached"]:
        LOG.warning("Output reached the token limit; inspect whether the answer is incomplete")
    return 1 if result["status"] == "pipeline_error" else 0

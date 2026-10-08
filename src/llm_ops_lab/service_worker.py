"""Private JSON-lines IPC worker. Only the supervisor starts this process."""

import argparse
import contextlib
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from llm_ops_lab.cli import MODEL_SPECS, generate_experiment, prepare_experiment, save_result
from llm_ops_lab.inference import InferenceRuntime, InputLimitError
from llm_ops_lab.retrieval import load_corpus, load_scope_contract

LOG = logging.getLogger(__name__)
MAX_INVESTIGATION_RESULT_BYTES = 48 * 1024


def reply(stream, message):
    stream.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


def process_request(config, request, runtime):
    settings = request["settings"]
    if settings.get("operation") == "olist_investigate":
        return process_olist_investigation(config, request, runtime)
    args = argparse.Namespace(
        prompt=settings["question"],
        context_file=None,
        corpus_dir=Path(config["corpus_dir"]),
        search_only=False,
        temperature=settings["temperature"],
        max_tokens=settings["max_tokens"],
        seed=settings["seed"],
        prompt_variant="baseline",
        answer_mode=settings["answer_mode"],
        model=config["model"],
        retriever="scoped-v3",
        operator="service",
        offline=True,
        output_dir=Path(config["output_dir"]),
    )
    started = time.perf_counter()
    result = None
    try:
        result = prepare_experiment(args)
        result.update(
            run_id=request["request_id"],
            request_id=request["request_id"],
            worker_pid=os.getpid(),
            service_schema_version="local-api.v1",
            service_source_sha256={
                name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ["service_worker.py", "service.py", "api.py"]
            },
        )
        if result["status"] == "prepared":
            generate_experiment(args, result, runtime)
        result["service_processing_seconds"] = time.perf_counter() - started
        try:
            save_result(result, args.output_dir)
        except OSError:
            return {
                "request_id": request["request_id"],
                "error": "storage_write_failed",
                "http_status": 500,
                "restart": False,
            }
        if result["status"] == "pipeline_error":
            input_limit = result.get("pipeline_error", {}).get("type") == "InputLimitError"
            return {
                "request_id": request["request_id"],
                "error": "input_token_limit" if input_limit else "generation_failed",
                "http_status": 422 if input_limit else 500,
                "restart": not input_limit,
            }
        return {"request_id": request["request_id"], "result": result}
    except Exception as error:
        # Exception details remain local. Never return prompts/paths/tracebacks over HTTP.
        if result is not None:
            result.update(
                status="pipeline_error",
                output="",
                error_type=type(error).__name__,
                error_message=str(error),
            )
            # Do not attempt to overwrite a completed record after a later failure.
            path = args.output_dir / f"{result['run_id']}.json"
            if not path.exists():
                try:
                    save_result(result, args.output_dir)
                except OSError:
                    pass
        print(f"Request {request['request_id']} failed: {type(error).__name__}", file=sys.stderr)
        return {
            "request_id": request["request_id"],
            "error": "input_token_limit" if isinstance(error, InputLimitError) else "worker_error",
            "http_status": 422 if isinstance(error, InputLimitError) else 500,
            "restart": not isinstance(error, InputLimitError),
        }


def process_olist_investigation(config, request, runtime):
    """Run the Olist agent inside the already-owned inference worker."""
    request_id = request["request_id"]
    root_value = config.get("olist_root")
    if not root_value:
        return {
            "request_id": request_id,
            "error": "investigation_config_unavailable",
            "http_status": 503,
            "restart": False,
        }

    settings = request["settings"]
    started = time.perf_counter()
    try:
        if config.get("olist_engine", "action-v5") == "intent-v1":
            from llm_ops_lab.olist.intent_agent import investigate
        else:
            from llm_ops_lab.olist.agent import investigate
        from llm_ops_lab.olist.config import connection_info

        dsn = connection_info(Path(root_value), reader=True)
    except Exception as error:
        LOG.warning(
            "Olist investigation setup failed request_id=%s type=%s",
            request_id,
            type(error).__name__,
        )
        return {
            "request_id": request_id,
            "error": "investigation_config_unavailable",
            "http_status": 503,
            "restart": False,
        }

    def generate(messages, timeout):
        # The Supervisor owns the hard deadline and kills this worker before it can
        # create a second GPU owner. The adapter preserves the lab's fixed generation.
        trace = {}
        generated = runtime.generate(messages, 256, trace, seed=42, temperature=0)
        if isinstance(generated, dict) and "output" in generated:
            return {
                **generated,
                "output": generated["output"],
                "prompt_tokens": generated.get("prompt_tokens", trace.get("prompt_tokens", 0)),
                "model_id": runtime.identity.get("model_id"),
                "model_revision": runtime.identity.get("model_revision"),
                "output_tokens": generated.get(
                    "output_tokens",
                    generated.get(
                        "generation_tokens", trace.get("generation_tokens", 0)
                    ),
                ),
            }
        return {
            **trace,
            "output": trace.get("output", ""),
            "prompt_tokens": trace.get("prompt_tokens", 0),
            "model_id": runtime.identity.get("model_id"),
            "model_revision": runtime.identity.get("model_revision"),
            "output_tokens": trace.get("generation_tokens", 0),
        }

    try:
        record = investigate(
            dsn,
            settings["question"],
            generate,
            version_id=settings.get("version_id"),
            timeout_seconds=float(config.get("request_timeout", 60.0)),
        )
        if not isinstance(record, dict) or record.get("status") not in {"completed", "abstained"}:
            raise ValueError("Investigator returned an invalid result")
        identity = dict(runtime.identity)
        result = {
            **record,
            "request_id": request_id,
            "run_id": request_id,
            "worker_pid": os.getpid(),
            "model_id": (record.get("model_id") or identity.get("model_id"))
            if record.get("model_executed") else None,
            "model_revision": (record.get("model_revision") or identity.get("model_revision"))
            if record.get("model_executed") else None,
            "model_executed": bool(record.get("model_executed", False)),
            "service_processing_seconds": time.perf_counter() - started,
        }
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_INVESTIGATION_RESULT_BYTES:
            return {
                "request_id": request_id,
                "error": "investigation_result_too_large",
                "http_status": 500,
                "restart": False,
            }
        try:
            save_result(result, Path(config["output_dir"]))
        except OSError:
            return {
                "request_id": request_id,
                "error": "storage_write_failed",
                "http_status": 500,
                "restart": False,
            }
        LOG.info(
            "Olist investigation request_id=%s dataset_version=%s stop_reason=%s "
            "model=%s revision=%s elapsed=%.3f executed=%s",
            request_id,
            result.get("dataset_version"),
            result.get("stop_reason"),
            result.get("model_id"),
            result.get("model_revision"),
            result["service_processing_seconds"],
            result["model_executed"],
        )
        return {"request_id": request_id, "result": result}
    except Exception as error:
        # Keep diagnostic content, DSNs, SQL, and traceback out of the HTTP response.
        LOG.error(
            "Olist investigation failed request_id=%s type=%s",
            request_id,
            type(error).__name__,
        )
        return {
            "request_id": request_id,
            "error": "investigation_failed",
            "http_status": 500,
            "restart": True,
        }


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    protocol = sys.stdout
    config = json.loads(sys.stdin.readline())
    # Keep library output away from the protocol stream.
    load_scope_contract(
        Path(config["corpus_dir"]), load_corpus(Path(config["corpus_dir"])), required=True
    )
    with contextlib.redirect_stdout(sys.stderr):
        runtime = InferenceRuntime(MODEL_SPECS[config["model"]], offline=True)
    reply(protocol, {"event": "ready", "pid": os.getpid(), "model": runtime.identity})
    for line in sys.stdin:
        request = json.loads(line)
        with contextlib.redirect_stdout(sys.stderr):
            response = process_request(config, request, runtime)
        reply(protocol, response)


if __name__ == "__main__":
    main()

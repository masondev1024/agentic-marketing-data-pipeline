"""Persistent MLX runtime; callers must serialize access within one owning process."""

import hashlib
import json
import os
import platform
import time
from importlib.metadata import version
from pathlib import Path

MAX_INPUT_TOKENS = 2048


class InputLimitError(ValueError):
    """The rendered request exceeds the model input budget."""


class InferenceRuntime:
    def __init__(self, model_spec: dict, *, offline: bool):
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("This first lab requires Apple Silicon macOS with the local extra")
        # Keep downloads within the project; never enable downloaded Python model code.
        os.environ.setdefault("HF_HOME", str(Path("work/huggingface").resolve()))
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        import mlx.core as mx
        from huggingface_hub import snapshot_download
        from mlx_lm import load, stream_generate
        from mlx_lm.sample_utils import make_sampler

        load_start = time.perf_counter()
        model_path = snapshot_download(
            repo_id=model_spec["id"],
            revision=model_spec["revision"],
            local_files_only=offline,
            allow_patterns=["*.json", "*.safetensors", "*.txt", "README.md"],
        )
        model, tokenizer = load(model_path, tokenizer_config={"trust_remote_code": False})
        load_seconds = time.perf_counter() - load_start

        self.model_spec = model_spec
        self.mx = mx
        self.stream_generate = stream_generate
        self.make_sampler = make_sampler
        self.model, self.tokenizer = model, tokenizer
        self.identity = {
            "model_id": model_spec["id"],
            "model_revision": model_spec["revision"],
            "mlx_lm_version": version("mlx-lm"),
            "mlx_version": version("mlx"),
            "load_seconds": load_seconds,
        }

    def generate(
        self, messages: list[dict], limit: int, trace: dict, *, seed: int, temperature: float
    ) -> dict:
        prompt_tokens = self.tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, enable_thinking=False
        )
        trace["prompt_tokens"] = len(prompt_tokens)
        trace["prompt_token_ids_sha256"] = hashlib.sha256(
            json.dumps(prompt_tokens, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if len(prompt_tokens) > MAX_INPUT_TOKENS:
            raise InputLimitError(f"Input exceeds the lab limit of {MAX_INPUT_TOKENS} tokens")
        self.mx.random.seed(seed)
        started = time.perf_counter()
        first_token_seconds = None
        parts = []
        last = None
        trace["model_executed"] = True
        try:
            for response in self.stream_generate(
                self.model,
                self.tokenizer,
                prompt=prompt_tokens,
                max_tokens=limit,
                sampler=self.make_sampler(temp=temperature),
            ):
                if first_token_seconds is None:
                    first_token_seconds = time.perf_counter() - started
                parts.append(response.text)
                last = response
        finally:
            elapsed = time.perf_counter() - started
            trace.update(
                output="".join(parts),
                generation_seconds=elapsed,
                generation_tokens=last.generation_tokens if last else 0,
                first_generation_event_seconds=first_token_seconds,
            )
        if last is None:
            raise RuntimeError("Model returned no generation events")
        trace.update(
            max_tokens_reached=last.generation_tokens >= limit,
            generation_tokens_per_second=last.generation_tokens / elapsed,
            mlx_peak_memory_gb=self.mx.get_peak_memory() / 1e9,
        )
        return trace

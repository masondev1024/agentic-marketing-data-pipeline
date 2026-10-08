"""Offline inference process; only the host starts it, never a model-selected tool."""

import json
import sys

from llm_ops_lab.cli import MODEL_SPECS
from llm_ops_lab.inference import InferenceRuntime


def main():
    runtime = InferenceRuntime(MODEL_SPECS["qwen3-4b"], offline=True)
    print(json.dumps({"ready": True, "identity": runtime.identity}), flush=True)
    for line in sys.stdin:
        try:
            trace = runtime.generate(json.loads(line), 256, {}, seed=42, temperature=0.0)
            print(json.dumps(trace), flush=True)
        except Exception as error:
            print(json.dumps({"error": type(error).__name__}), flush=True)


if __name__ == "__main__":
    main()

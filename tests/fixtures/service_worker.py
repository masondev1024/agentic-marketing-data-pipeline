"""Deterministic IPC fault fixture. Never used by the public server entrypoint."""

import json
import os
import sys
import time
from pathlib import Path

config = json.loads(sys.stdin.readline())
if len(sys.argv) > 1 and sys.argv[1] == "startup-hang":
    time.sleep(30)
identity = {"model_id": "fixture", "model_revision": "fixture", "load_seconds": 0}
print(json.dumps({"event": "ready", "pid": os.getpid(), "model": identity}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    question = request["settings"]["question"]
    with (Path(config["output_dir"]) / "fixture-started.jsonl").open("a") as handle:
        handle.write(json.dumps(request) + "\n")
    if question == "hang":
        time.sleep(30)
    if question == "crash":
        os._exit(7)
    if question == "slow":
        time.sleep(0.3)
    if question == "invalid-ipc":
        print("{bad", flush=True)
        continue
    result = {
        "request_id": request["request_id"],
        "status": "generated",
        "output": "fixture answer",
        "answer_mode": "direct",
        "sources": [],
        "model_executed": True,
        "model_id": "fixture",
        "model_revision": "fixture",
        "worker_pid": os.getpid(),
        "generation_seconds": 0.3 if question == "slow" else 0,
    }
    print(json.dumps({"request_id": request["request_id"], "result": result}), flush=True)

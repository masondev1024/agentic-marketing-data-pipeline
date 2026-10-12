"""Explicit hosted intent service configuration; never a local-model fallback."""

import os
import stat
from pathlib import Path

from .nvidia_planner import NvidiaPlanner, NvidiaPlannerError

MODEL_ID = "nvidia/nemotron-3-super-120b-a12b"
CALLS_PER_REQUEST = 2


def load_api_key(root: Path) -> str:
    """Use the environment or the existing owner-only local key file, without echoing it."""
    key = os.environ.get("NVIDIA_API_KEY")
    if key is not None:
        if not key.strip():
            raise NvidiaPlannerError("credentials_missing")
        return key.strip()
    path = root / "work/olist/secrets/nvidia-api-key.txt"
    try:
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 4096
            ):
                raise NvidiaPlannerError("credentials_file_unsafe")
            key = handle.read(4097).decode("utf-8").strip()
    except (OSError, UnicodeError):
        raise NvidiaPlannerError("credentials_unavailable") from None
    if not key:
        raise NvidiaPlannerError("credentials_missing")
    return key


def create_planner(root: Path) -> NvidiaPlanner:
    # Parent reservations bound all child lifetimes. The adapter is reused within a worker.
    return NvidiaPlanner(model_id=MODEL_ID, api_key=load_api_key(root), max_total_calls=128)


def error_status(code: str) -> int:
    if code == "upstream_timeout":
        return 504
    if code in {"rate_limited", "authentication_failed", "call_budget_exhausted"}:
        return 503
    return 502

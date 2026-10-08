"""Bounded single-worker supervisor with kill/reap before restart."""

import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from llm_ops_lab.cli import MODEL_SPECS

LOG = logging.getLogger(__name__)
ACCESS_LOCK = threading.Lock()


@dataclass(frozen=True)
class ServiceConfig:
    corpus_dir: Path = Path("data/runbooks")
    output_dir: Path = Path("runs/service")
    model: str = "qwen3-4b"
    queue_size: int = 2
    request_timeout: float = 60
    startup_timeout: float = 120
    max_restarts: int = 3
    olist_root: Path | None = None
    olist_engine: str = "action-v5"

    def __post_init__(self):
        if self.olist_engine not in {"action-v5", "intent-v1"}:
            raise ValueError("Unknown Olist engine")
        if self.model not in MODEL_SPECS:
            raise ValueError("Unknown model profile")
        if not 0 <= self.queue_size <= 8 or not 0 <= self.max_restarts <= 10:
            raise ValueError("Invalid queue or restart budget")
        for value in [self.request_timeout, self.startup_timeout]:
            if not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError("Timeout must be finite, positive and at most 300 seconds")


class ServiceError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass
class Job:
    request_id: str
    settings: dict
    deadline: float
    created: float
    future: asyncio.Future
    abandoned: bool = False


class Supervisor:
    def __init__(self, config: ServiceConfig, *, worker_command=None):
        self.config = config
        self.command = worker_command or [sys.executable, "-m", "llm_ops_lab.service_worker"]
        self.queue = asyncio.Queue()
        self.jobs = {}
        self.process = None
        self.task = None
        self.active_id = None
        self.state = "starting"
        self.restarts = 0
        self.worker_identity = None
        self.counters = {
            "accepted": 0,
            "completed": 0,
            "timeouts": 0,
            "overloaded": 0,
            "worker_failures": 0,
        }
        self.stderr = None

    @property
    def ready(self):
        return (
            self.state == "ready" and self.process is not None and self.process.returncode is None
        )

    def health(self):
        return {
            "ready": self.ready,
            "state": self.state,
            "worker_pid": (
                self.process.pid if self.process and self.process.returncode is None else None
            ),
            "active": int(self.active_id is not None),
            "queued": len(self.jobs) - int(self.active_id is not None),
            "capacity": 1 + self.config.queue_size,
            "restarts": self.restarts,
            "counters": dict(self.counters),
        }

    def start(self):
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.stderr = (self.config.output_dir / "worker.stderr.log").open("ab", buffering=0)
        self.task = asyncio.create_task(self._serve(), name="inference-supervisor")

    async def close(self):
        self.state = "stopping"
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self._fail_pending("service_stopping")
        await self._stop_worker()
        if self.stderr:
            self.stderr.close()
        self.state = "stopped"

    async def submit(self, request_id, settings):
        if not self.ready:
            raise ServiceError(503, "worker_unavailable")
        if len(self.jobs) >= 1 + self.config.queue_size:
            self.counters["overloaded"] += 1
            raise ServiceError(429, "capacity_exceeded")
        now = time.monotonic()
        job = Job(
            request_id,
            settings,
            now + self.config.request_timeout,
            now,
            asyncio.get_running_loop().create_future(),
        )
        self.jobs[request_id] = job
        self.queue.put_nowait(job)
        self.counters["accepted"] += 1
        try:
            # Client cancellation cannot free the GPU slot while work is still running.
            return await asyncio.shield(job.future)
        except asyncio.CancelledError:
            job.abandoned = True
            job.future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            raise

    def _finish(self, job, *, result=None, error=None):
        self.jobs.pop(job.request_id, None)
        if not job.future.done():
            if error:
                job.future.set_exception(error)
            else:
                job.future.set_result(result)

    def _fail_pending(self, code):
        for job in list(self.jobs.values()):
            self._finish(job, error=ServiceError(503, code))
        while not self.queue.empty():
            self.queue.get_nowait()
        self.active_id = None

    async def _stop_worker(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), 3)
        if self.process.stdin:
            self.process.stdin.close()
        self.process = None

    async def _boot(self):
        self.state = "starting" if self.restarts == 0 else "recovering"
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr,
            limit=1024 * 1024,
        )
        config = {
            "corpus_dir": str(self.config.corpus_dir.resolve()),
            "output_dir": str(self.config.output_dir.resolve()),
            "model": self.config.model,
            "request_timeout": self.config.request_timeout,
            "olist_engine": self.config.olist_engine,
            "olist_root": str(self.config.olist_root.resolve()) if self.config.olist_root else None,
        }
        self.process.stdin.write((json.dumps(config) + "\n").encode())
        await self.process.stdin.drain()
        raw = await asyncio.wait_for(self.process.stdout.readline(), self.config.startup_timeout)
        message = json.loads(raw)
        if message.get("event") != "ready" or message.get("pid") != self.process.pid:
            raise RuntimeError("Invalid worker readiness")
        self.worker_identity = message["model"]
        self.state = "ready"
        LOG.info("Worker ready pid=%s restart=%s", self.process.pid, self.restarts)

    async def _recover(self):
        self.state = "recovering"
        self._fail_pending("worker_recovering")
        await self._stop_worker()  # Must finish before another GPU owner is created.
        if self.restarts >= self.config.max_restarts:
            self.state = "failed"
            return False
        self.restarts += 1
        return True

    async def _execute(self, job):
        self.active_id = job.request_id
        remaining = job.deadline - time.monotonic()
        if remaining <= 0 or job.abandoned:
            self._finish(job, error=ServiceError(504, "queue_deadline_exceeded"))
            self.counters["timeouts"] += 1
            self.active_id = None
            return False
        queued_seconds = time.monotonic() - job.created
        try:
            async with asyncio.timeout(remaining):
                self.process.stdin.write(
                    (
                        json.dumps(
                            {"request_id": job.request_id, "settings": job.settings},
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode()
                )
                await self.process.stdin.drain()
                raw = await self.process.stdout.readline()
                response = json.loads(raw)
                if response.get("request_id") != job.request_id:
                    raise RuntimeError("Worker response ID mismatch")
        except TimeoutError:
            self.counters["timeouts"] += 1
            self._finish(job, error=ServiceError(504, "inference_deadline_exceeded"))
            return True
        except (OSError, ValueError, RuntimeError):
            self.counters["worker_failures"] += 1
            self._finish(job, error=ServiceError(503, "worker_crashed"))
            return True
        finally:
            self.active_id = None
        if "error" in response:
            self._finish(job, error=ServiceError(response["http_status"], response["error"]))
            return response.get("restart", False)
        result = response["result"]
        result["queue_seconds"] = queued_seconds
        result["request_seconds"] = time.monotonic() - job.created
        self.counters["completed"] += 1
        self._finish(job, result=result)
        return False

    async def _serve(self):
        try:
            while True:
                try:
                    await self._boot()
                    while True:
                        if self.process.returncode is not None:
                            raise RuntimeError("Worker exited while idle")
                        try:
                            job = await asyncio.wait_for(self.queue.get(), 0.1)
                        except TimeoutError:
                            continue
                        if await self._execute(job):
                            break
                except (OSError, ValueError, RuntimeError, TimeoutError):
                    LOG.exception("Worker failure; recovering within restart budget")
                    self.counters["worker_failures"] += 1
                if not await self._recover():
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state = "failed"
            self._fail_pending("supervisor_failed")
            LOG.exception("Supervisor failed; no new worker will be started")
        finally:
            await self._stop_worker()


def append_access_event(path, event):
    with ACCESS_LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

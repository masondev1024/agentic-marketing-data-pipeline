"""Serialized owned inference worker with bounded frames and hard host deadlines."""

import fcntl
import json
import math
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from llm_ops_lab.marketing.contracts import strict_json

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 128 * 1024


class OfflineModel:
    kind = "actual_offline_mlx"

    def __init__(self):
        self.lock = self.stderr = self.process = None
        self._buffer = bytearray()

    def __enter__(self):
        try:
            self.lock = (Path(tempfile.gettempdir()) / "llm-ops-marketing-inference.lock").open("a")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("marketing inference worker already active") from None
            self.stderr = tempfile.TemporaryFile(mode="w+")
            self.process = subprocess.Popen(
                [sys.executable, "-m", "llm_ops_lab.marketing.model_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                bufsize=0)
            self.identity = self._read(120)["identity"]
            return self
        except BaseException:
            # Cancellation during model loading must release the owned process and lock.
            self.__exit__(None, None, None)
            raise

    def _stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()

    def _deadline(self, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            self._stop()
            raise TimeoutError("offline model deadline")
        return time.monotonic() + timeout

    def _wait(self, stream, deadline, *, writing=False):
        remaining = deadline - time.monotonic()
        ready = False
        if remaining > 0:
            read, write, _ = select.select([] if writing else [stream],
                                          [stream] if writing else [], [], remaining)
            ready = bool(write if writing else read)
        if not ready:
            self._stop()
            raise TimeoutError("offline model deadline")

    def _read_until(self, deadline):
        stream = self.process.stdout
        os.set_blocking(stream.fileno(), False)
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_RESPONSE_BYTES:
                    self._stop()
                    raise ValueError("offline model response frame too large")
                raw = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                value = strict_json(raw)
                if type(value) is not dict:
                    raise ValueError("offline model response must be an object")
                if "error" in value:
                    raise ValueError(value["error"])
                return value
            if len(self._buffer) > MAX_RESPONSE_BYTES:
                self._stop()
                raise ValueError("offline model response frame too large")
            self._wait(stream, deadline)
            try:
                chunk = os.read(stream.fileno(), 4096)
            except BlockingIOError:
                continue
            if not chunk:
                raise ValueError("offline model response ended before complete frame")
            self._buffer.extend(chunk)

    def _read(self, timeout):
        return self._read_until(self._deadline(timeout))

    def generate(self, messages, timeout):
        deadline = self._deadline(timeout)
        raw = (json.dumps(messages, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("offline model request frame too large")
        stream = self.process.stdin
        os.set_blocking(stream.fileno(), False)
        sent = 0
        while sent < len(raw):
            self._wait(stream, deadline, writing=True)
            try:
                sent += os.write(stream.fileno(), raw[sent:sent + 4096])
            except BlockingIOError:
                continue
        return self._read_until(deadline)

    def __exit__(self, *_):
        try:
            self._stop()
        finally:
            streams = [self.stderr, self.lock]
            if self.process is not None:
                streams += [self.process.stdin, self.process.stdout, self.process.stderr]
            for stream in streams:
                if stream is not None:
                    stream.close()

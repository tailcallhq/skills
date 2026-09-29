"""Process helpers: explicit cwd, bounded readiness polling, and guaranteed cleanup.

Every child runs in its own process group (POSIX) or process-group-like job
(Windows CREATE_NEW_PROCESS_GROUP) so a timeout kills the whole tree, not just
the direct child. Nothing here inherits the caller's cwd implicitly: `cwd` is a
required argument.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

IS_WINDOWS = os.name == "nt"


def _popen_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def run(cmd: list[str], cwd: str | Path, timeout: float = 60, env: dict | None = None,
        input: str | None = None) -> subprocess.CompletedProcess:
    """Run to completion with an explicit cwd; kill the process group on timeout."""
    cwd = Path(cwd)
    if not cwd.is_dir():
        raise FileNotFoundError(f"cwd does not exist: {cwd}")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True,
                            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_popen_kwargs())
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate(proc)
        out, err = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def terminate(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """Stop a child and its group: polite signal, then kill after `grace` seconds."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if IS_WINDOWS:
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    proc.wait(timeout=grace)


@contextmanager
def background(cmd: list[str], cwd: str | Path, log_path: str | Path,
               env: dict | None = None) -> Iterator[subprocess.Popen]:
    """Start a long-running process (dev server etc.) and always reap it."""
    cwd = Path(cwd)
    if not cwd.is_dir():
        raise FileNotFoundError(f"cwd does not exist: {cwd}")
    log = open(log_path, "w")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, **_popen_kwargs())
    try:
        yield proc
    finally:
        terminate(proc)
        log.close()


def free_port() -> int:
    """An OS-assigned free localhost port (avoids fixed-port collisions)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check: Callable[[], bool], timeout: float = 30, interval: float = 0.25,
               proc: subprocess.Popen | None = None) -> float:
    """Poll `check` until true; return seconds waited. Fails fast if `proc` exits."""
    start = time.monotonic()
    while True:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"process exited early with code {proc.returncode}")
        try:
            if check():
                return time.monotonic() - start
        except Exception:
            pass
        if time.monotonic() - start > timeout:
            raise TimeoutError(f"not ready after {timeout}s")
        time.sleep(interval)


def http_ok(url: str) -> Callable[[], bool]:
    def check() -> bool:
        with urllib.request.urlopen(url, timeout=2) as r:
            return 200 <= r.status < 500
    return check

"""gh api layer for repo_graph.py (imported lazily; stdlib only; no tokens handled here).

All calls go through `gh api --include` so rate-limit headers are visible.
Failures are returned as (None, error_string) and recorded in the repo's errors[].
"""
import json
import os
import subprocess
import sys
import threading
import time

LOW_WATER = 50          # same rule as detect_tracker.sh
MAX_SLEEP = 60          # never block longer than this per backoff


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def parse_include(text):
    """Split `gh api --include` output into (status, headers, body_text)."""
    status, headers = 0, {}
    head, sep, body = text.partition("\r\n\r\n")
    if not sep:
        head, sep, body = text.partition("\n\n")
    lines = head.splitlines()
    if lines and lines[0].startswith("HTTP/"):
        parts = lines[0].split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    else:
        body = text
    return status, headers, body


class Gh:
    """Thread-safe `gh api` wrapper with rate-limit backoff and call counting."""

    def __init__(self, gh="gh", low_water=LOW_WATER, max_sleep=MAX_SLEEP, sleep=time.sleep):
        self.gh, self.low_water, self.max_sleep, self.sleep = gh, low_water, max_sleep, sleep
        self.remaining, self.reset = None, None
        self.calls, self.backoffs = 0, 0
        self._lock = threading.Lock()

    def _maybe_backoff(self):
        with self._lock:
            rem, reset = self.remaining, self.reset
        if rem is None or rem >= self.low_water:
            return
        wait = max(0, min(self.max_sleep, (reset or time.time()) - time.time()))
        log(f"[repo_graph] gh rate limit low ({rem} remaining); backing off {wait:.0f}s")
        with self._lock:
            self.backoffs += 1
        self.sleep(wait)

    def api(self, endpoint, *args):
        """Return (data, None) or (None, "error"). Never raises."""
        self._maybe_backoff()
        cmd = [self.gh, "api", "--include", *args, endpoint]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                               env={**os.environ, "GH_PROMPT_DISABLED": "1"})
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"gh api {endpoint}: {exc}"
        status, headers, body = parse_include(p.stdout)
        with self._lock:
            self.calls += 1
            if "x-ratelimit-remaining" in headers:
                try:
                    self.remaining = int(headers["x-ratelimit-remaining"])
                    self.reset = int(headers.get("x-ratelimit-reset", 0)) or None
                except ValueError:
                    pass
        if p.returncode != 0 or status >= 400:
            msg = ""
            try:
                msg = json.loads(body).get("message", "")
            except (ValueError, AttributeError):
                msg = (p.stderr or body).strip().splitlines()[-1:] and (p.stderr or body).strip().splitlines()[-1]
            return None, f"gh api {endpoint}: HTTP {status or '?'} {msg}".strip()
        try:
            data = json.loads(body) if body.strip() else None
        except ValueError as exc:
            return None, f"gh api {endpoint}: bad JSON: {exc}"
        if isinstance(data, dict) and data.get("errors") and not data.get("data"):
            return None, f"gh api {endpoint}: {data['errors'][0].get('message', 'graphql error')}"
        return data, None

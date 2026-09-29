"""Redact secret-looking values from text before it is written to evidence.

This is a best-effort filter for logs and transcripts, not a guarantee. The
harness never reads secret stores; redaction exists because tool output can
still echo environment variables or tokens.
"""

from __future__ import annotations

import os
import re

MASK = "[REDACTED]"

_PATTERNS = [
    re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic|token)\s+)[^\s\"']+"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIALS?)[A-Z0-9_]*\s*[=:]\s*)[\"']?[^\s\"']+"),
    re.compile(r"(?i)(\"(?:[a-z_]*?)(?:token|secret|password|api_?key)\"\s*:\s*\")[^\"]+"),
    re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}"),
    re.compile(r"\b(github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(sk-(?:ant-|proj-)?)[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(AKIA)[A-Z0-9]{16}"),
    re.compile(r"\b(xox[abpr]-)[A-Za-z0-9\-]{10,}"),
    re.compile(r"\b(eyJ)[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"),
]
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_SECRET_ENV = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|API_?KEY|CREDENTIAL)")


def redact(text: str, extra_values: list[str] | None = None) -> str:
    if not text:
        return text
    text = _PEM.sub(MASK, text)
    for pat in _PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: m.group(1) + MASK + m.group(2), text)
        else:
            text = pat.sub(lambda m: m.group(1) + MASK, text)
    values = list(extra_values or [])
    values += [v for k, v in os.environ.items() if _SECRET_ENV.search(k) and len(v) >= 8]
    for v in sorted(set(values), key=len, reverse=True):
        if v:
            text = text.replace(v, MASK)
    return text


def safe_env(base: dict | None = None) -> dict:
    """A child environment with secret-looking variables removed."""
    env = dict(os.environ if base is None else base)
    return {k: v for k, v in env.items() if not _SECRET_ENV.search(k)}

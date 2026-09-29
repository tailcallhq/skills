"""Read-only inventory of the *target* forge3 runtime, plus direct loader probes.

Everything here talks to `forge3 stdio` (newline-delimited JSON-RPC 2.0) with
requests that do not start a model turn: rpc.discover, info, extension_list,
tool_list, skill_list, command_list, and `tool_call` for skill_view /
skill_search. No tokens are spent and no settings are changed.

The installed binary and a local SDK checkout are not necessarily the same
build, so the inventory records the binary path and reported version, and
callers must decide capability from this output rather than from source.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import proc
from .redact import redact

BINARY = os.environ.get("FORGE_BIN", "forge3")

# Classification of what a name in the schema actually is. Only `tool` entries
# are callable by an agent; the rest need a host/UI integration.
CALLABLE_TOOL = "callable_tool"
SDK_METHOD = "sdk_method_only"


class RuntimeError_(RuntimeError):
    pass


def binary_path() -> str | None:
    return shutil.which(BINARY)


def rpc_batch(frames: list[dict], cwd: str | Path, timeout: float = 60) -> dict[str, dict]:
    """Send frames in one stdio session and return responses keyed by id."""
    path = binary_path()
    if not path:
        raise RuntimeError_(f"{BINARY} not found on PATH (set FORGE_BIN)")
    payload = "".join(json.dumps(f) + "\n" for f in frames)
    cp = proc.run([path, "stdio"], cwd=cwd, timeout=timeout, input=payload)
    out: dict[str, dict] = {}
    for line in cp.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in msg and msg["id"] is not None:
            out[str(msg["id"])] = msg
    return out


class Session:
    """One long-lived `forge3 stdio` process for stateful checks (e.g. reload)."""

    def __init__(self, cwd: str | Path, timeout: float = 30) -> None:
        path = binary_path()
        if not path:
            raise RuntimeError_(f"{BINARY} not found on PATH (set FORGE_BIN)")
        import subprocess as sp
        self.timeout = timeout
        self.p = sp.Popen([path, "stdio"], cwd=cwd, stdin=sp.PIPE, stdout=sp.PIPE,
                          stderr=sp.DEVNULL, text=True, bufsize=1, **proc._popen_kwargs())

    def call(self, frame: dict) -> dict:
        import select
        self.p.stdin.write(json.dumps(frame) + "\n")
        self.p.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if os.name != "nt":
                r, _, _ = select.select([self.p.stdout], [], [], max(0.0, deadline - time.monotonic()))
                if not r:
                    break
            line = self.p.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(msg.get("id")) == str(frame["id"]):
                return msg
        raise TimeoutError(f"no response to {frame.get('method')} within {self.timeout}s")

    def call_stream(self, frame: dict) -> list[dict]:
        """Send an `<method>/xstream` request and collect items until the stream completes.

        Needed for multi-step commands: plain (non-stream) `command_execute`
        returns only the first frame, so later steps such as the actual
        `skill.reload` never run (observed on forge3 0.21.0).
        """
        import select
        self.p.stdin.write(json.dumps(frame) + "\n")
        self.p.stdin.flush()
        items: list[dict] = []
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if os.name != "nt":
                r, _, _ = select.select([self.p.stdout], [], [], max(0.0, deadline - time.monotonic()))
                if not r:
                    break
            line = self.p.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            st = (msg.get("params") or {}).get("stream") or {}
            if str(st.get("x-stream-request-id")) != str(frame["id"]):
                continue
            if "error" in st:
                raise RuntimeError_(json.dumps(st["error"]))
            if "complete" in st:
                return items
            if "result" in st:
                items.append(st["result"])
        raise TimeoutError(f"stream {frame.get('method')} did not complete within {self.timeout}s")

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        proc.terminate(self.p)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _complete(msg: dict | None, key: str):
    if not msg or "error" in msg:
        return None
    return msg.get("result", {}).get("data", {}).get("complete", {}).get(key)


def tool_call_frame(req_id: str, name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tool_call", "params": {
        "agent_id": "forge", "conversation_id": f"harness-probe-{req_id}",
        "model_id": "none", "provider_id": "none",
        "tool_call": {"id": req_id, "name": name, "arguments": arguments}}}


def tool_call_result(msg: dict | None) -> tuple[str, str | None]:
    """(text, error_message) from a tool_call response."""
    if not msg:
        return "", "no response"
    if "error" in msg:
        return "", msg["error"].get("message")
    tc = _complete(msg, "tool_call") or {}
    text = "\n".join(p.get("text", "") for p in tc.get("result") or [] if isinstance(p, dict))
    err = (tc.get("error") or {}).get("message") if tc.get("error") else None
    return text, err


def inventory(cwd: str | Path) -> dict:
    """Snapshot the runtime. Output is redacted and safe to store as evidence."""
    started = time.monotonic()
    names = ["rpc.discover", "info", "extension_list", "tool_list", "skill_list", "command_list"]
    frames = []
    for n in names:
        f = {"jsonrpc": "2.0", "id": n, "method": n}
        if n == "tool_list":
            f["params"] = {}
        frames.append(f)
    resp = rpc_batch(frames, cwd)

    discover = resp.get("rpc.discover", {}).get("result") or {}
    discover = discover.get("data", {}).get("complete", {}).get("rpc.discover", discover)
    methods = sorted(m["name"] for m in discover.get("methods", []))
    schemas = discover.get("components", {}).get("schemas", {})
    ext_req = schemas.get("ExtensionRequest", {})
    ext_variants = len(ext_req.get("oneOf") or ext_req.get("anyOf") or [])

    exts = (_complete(resp.get("extension_list"), "extension_list") or {}).get("extensions", [])
    tools = (_complete(resp.get("tool_list"), "tool_list") or {}).get("tools", [])
    skills = (_complete(resp.get("skill_list"), "skill_list") or {}).get("skills", [])
    commands = (_complete(resp.get("command_list"), "command_list") or {}).get("commands", [])
    info_msg = resp.get("info")

    tool_names = sorted({t.get("name") for t in tools if t.get("name")})
    ext_summary = []
    for e in exts:
        data = e.get("data") or {}
        ext_summary.append({"id": e.get("id"), "enabled": e.get("enabled"),
                            "capabilities": data.get("capabilities", [])})

    inv = {
        "binary": binary_path(),
        "version": (discover.get("info") or {}).get("version"),
        "transport": [s.get("name") for s in discover.get("servers", [])],
        "elapsed_s": round(time.monotonic() - started, 2),
        "rpc_methods": methods,
        "extension_request_variants": ext_variants,
        "extensions": ext_summary,
        "tools": tool_names,
        "skills": [{"name": s.get("name"), "source": s.get("source"), "path": s.get("path")} for s in skills],
        "commands": sorted(c.get("id") for c in commands if c.get("id")),
        "info_ok": bool(info_msg and "error" not in info_msg),
        "errors": {k: v["error"].get("message") for k, v in resp.items() if "error" in v},
    }
    inv["classification"] = classify(inv)
    return json.loads(redact(json.dumps(inv)))


# Methods that look like features but have no agent tool: calling them needs a
# host/UI integration. Filled from the observed inventory, not assumed.
_SDK_ONLY_HINTS = ("terminal_", "routine_", "secret_", "confirm_", "mcp_server_",
                   "custom_provider_", "extension_set_enabled", "extension_config_set")

# Capabilities that are platform/transport-gated or external and must be probed,
# never assumed. Keys are the evidence we look for in the inventory.
GATES = {
    "workflow": {"kind": "platform_gated", "tool": "workflow"},
    "semantic_search": {"kind": "auth_gated", "tool": "sem_search"},
    "relay": {"kind": "transport_gated", "extension": "svc.relay"},
    "browser_automation": {"kind": "external", "binaries": ["playwright", "chromium", "google-chrome"]},
    "mobile_driver": {"kind": "external", "binaries": ["adb", "xcrun", "emulator"]},
    "container_runtime": {"kind": "external", "binaries": ["docker", "podman"]},
    "gui_display": {"kind": "external", "env": ["DISPLAY", "WAYLAND_DISPLAY"]},
}


def classify(inv: dict) -> dict:
    tools = set(inv["tools"])
    exts = {e["id"] for e in inv["extensions"] if e.get("enabled")}
    sdk_only = [m for m in inv["rpc_methods"] if m.startswith(_SDK_ONLY_HINTS) or m in _SDK_ONLY_HINTS]
    gates = {}
    for name, g in GATES.items():
        if "tool" in g:
            present = g["tool"] in tools
            status = "present_unverified" if present and g["kind"] == "auth_gated" else ("present" if present else "absent")
        elif "extension" in g:
            status = "present" if g["extension"] in exts else "absent"
        elif "binaries" in g:
            found = [b for b in g["binaries"] if shutil.which(b)]
            status = "present_unverified" if found else "absent"
        else:
            found = [e for e in g["env"] if os.environ.get(e)]
            status = "present_unverified" if found else "absent"
        gates[name] = {"kind": g["kind"], "status": status}
    return {"callable_tools": sorted(tools), "sdk_only_methods": sdk_only,
            "registered_services": sorted(exts), "gates": gates}


def require(inv: dict, tool: str | None = None, gate: str | None = None) -> tuple[bool, str]:
    """Capability check for a scenario. Returns (ok, reason). Absent == blocker, never a pass."""
    if tool and tool not in inv["classification"]["callable_tools"]:
        return False, f"tool {tool!r} not callable in this runtime"
    if gate:
        st = inv["classification"]["gates"].get(gate, {}).get("status", "unknown")
        if st != "present":
            return False, f"gate {gate!r} status={st}"
    return True, "ok"

#!/usr/bin/env python3
# Modified by Tailcall for Forge, 2026 — original: anthropics/skills
# (New file added by Tailcall.)
"""Minimal stdlib-only client for the `forge3` agent runner.

`forge3` serves newline-delimited JSON-RPC 2.0 over stdio (`forge3 stdio`): one
request frame per line on stdin, one response or notification frame per line on
stdout. A one-shot prompt is therefore a single conversation request whose
stream we read to completion — that is what `run_prompt` does.

The request is `conversation/xstream`. The `/xstream` suffix is what selects
the streaming response shape; without it only the first item would come back.
The server answers with a stream *pointer* frame, then a sequence of
notifications:

    {"jsonrpc":"2.0","method":"conversation/xstream","params":{"stream":{
        "x-stream-request-id":"1","x-stream-seq-id":0,
        "result":{"conversation":{"conversation_id":"...","update":<update>}}}}}

`<update>` is a serde-tagged `ConversationUpdate`: the unit variant `"start"`
as a bare string, and everything else as a single-key object — `{"chat_chunk":
{"content": "..."}}`, `{"tool_call": {"id":..., "name":..., "arguments":...}}`,
`{"tool_result": {...}}`, `{"snapshot": {...}}`, and so on. There is no end
flag: the payload key of the stream envelope is what terminates it — `result`
means "another item", `error` is a terminal error, and `complete` is the
terminal sentinel after the last value.

Assistant text arrives as `chat_chunk` deltas, which we concatenate. Tool calls
are announced by the `tool_call` update *before* the tool runs, which is what
makes trigger detection cheap: a skill has triggered as soon as the agent calls
`skill_view` with that skill's name.
"""

import json
import os
import subprocess
import threading
from pathlib import Path

DEFAULT_BINARY = os.environ.get("FORGE_BIN", "forge3")
DEFAULT_AGENT = os.environ.get("FORGE_AGENT_ID", "forge")

# The model and provider are deliberately *not* defaulted to a hardcoded name.
# `model_id`/`provider_id` are required strings on the wire, and the right
# values are the ones powering the session that invoked this skill: the whole
# point of a trigger eval is to measure what the user will actually experience.
# Guessing from the catalogue also risks picking an arbitrarily expensive
# model. Callers pass them explicitly; these env vars are the fallback.
DEFAULT_PROVIDER = os.environ.get("FORGE_PROVIDER_ID")
DEFAULT_MODEL = os.environ.get("FORGE_MODEL_ID")

# Tool names that mean "the agent went and consulted a skill".
SKILL_TOOLS = ("skill_view", "skill_search")


class ForgeError(RuntimeError):
    """The runner refused the request or failed mid-stream."""


def _rpc(frame: str, binary: str, timeout: int = 60) -> dict:
    """Send one non-streaming request and return its `complete` payload."""
    process = subprocess.run(
        [binary, "stdio"],
        input=frame + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    for line in process.stdout.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue  # forge3 also emits structured log lines
        if parsed.get("id") != "1":
            continue
        if "error" in parsed:
            raise ForgeError(json.dumps(parsed["error"]))
        return parsed.get("result", {}).get("data", {}).get("complete", {})
    raise ForgeError("no response frame from forge3")


def list_models(binary: str | None = None) -> list[dict]:
    """Every model the runner knows about, as `{provider_id, model_id, ...}`.

    `model_list` takes no params, so the frame omits the key entirely —
    sending `{}` fails with `invalid type: map, expected unit struct`.
    """
    frame = json.dumps({"jsonrpc": "2.0", "id": "1", "method": "model_list"})
    payload = _rpc(frame, binary or DEFAULT_BINARY)
    return payload.get("model_list", {}).get("models", [])




def _request(prompt: str, cwd: str, model: str, provider: str,
             agent: str | None) -> str:
    """Build the single-line JSON-RPC frame that starts a conversation.

    `conversation_id: null` means "create a new conversation", so each call is
    independent. The working directory is passed both as the child process cwd
    and as a `working_directory` attachment, because the skill loader resolves
    a turn's project skill directories from those attachments.
    """
    params = {
        "agent_id": agent or DEFAULT_AGENT,
        "conversation_id": None,
        "model_id": model,
        "provider_id": provider,
        "content": prompt,
        "attachments": [{"working_directory": {"path": str(Path(cwd).resolve())}}],
    }
    return json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "method": "conversation/xstream",
        "params": params,
    })


def _extract_update(frame: dict) -> dict | None:
    """Return the `ConversationUpdate` carried by a stream notification."""
    stream = frame.get("params", {}).get("stream")
    if not stream:
        return None
    if "error" in stream:
        raise ForgeError(json.dumps(stream["error"]))
    if "complete" in stream:
        return None
    result = stream.get("result") or {}
    conversation = result.get("conversation") or {}
    update = conversation.get("update")
    return update if isinstance(update, dict) else None


def run_prompt(
    prompt: str,
    cwd: str,
    timeout: int = 120,
    model: str | None = None,
    provider: str | None = None,
    agent: str | None = None,
    binary: str | None = None,
) -> dict:
    """Run one prompt to completion and return `{"text", "tool_calls"}`.

    `text` is the concatenated assistant output; `tool_calls` is a list of
    `{"name", "arguments"}` in the order the agent announced them. Raises
    `ForgeError` if the runner reports an error frame; returns whatever was
    collected so far if the timeout expires (the child is always killed).
    """
    binary = binary or DEFAULT_BINARY
    model = model or DEFAULT_MODEL
    provider = provider or DEFAULT_PROVIDER
    if not model or not provider:
        raise ForgeError(
            "both a model and a provider are required: pass --model/--provider "
            "(or set FORGE_MODEL_ID/FORGE_PROVIDER_ID) using the model and "
            "provider powering the calling session. `list_models()` enumerates "
            "the valid pairs."
        )

    cmd = [binary, "stdio"]
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
        text=True,
        bufsize=1,
    )

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    failure: list[BaseException] = []

    def pump() -> None:
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    # forge3 also writes structured log lines; ignore non-frames.
                    continue
                if "error" in frame and "id" in frame:
                    raise ForgeError(json.dumps(frame["error"]))
                stream = frame.get("params", {}).get("stream")
                update = _extract_update(frame)
                if update is None:
                    # Terminal sentinel (`complete`) or a non-stream frame.
                    if stream is not None and "complete" in stream:
                        return
                    continue
                if "chat_chunk" in update:
                    chunk = update["chat_chunk"] or {}
                    if chunk.get("content"):
                        text_parts.append(chunk["content"])
                elif "tool_call" in update:
                    call = update["tool_call"] or {}
                    tool_calls.append({
                        "name": call.get("name", ""),
                        "arguments": call.get("arguments", {}),
                    })
        except BaseException as exc:  # surfaced on the calling thread
            failure.append(exc)

    reader = threading.Thread(target=pump, daemon=True)
    try:
        process.stdin.write(_request(prompt, cwd, model, provider, agent) + "\n")
        process.stdin.flush()
        reader.start()
        reader.join(timeout)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    if failure:
        raise failure[0]

    return {"text": "".join(text_parts), "tool_calls": tool_calls}


def called_skill(result: dict, skill_name: str) -> bool:
    """Whether the run consulted `skill_name` — the trigger signal.

    The authoritative signal is a `skill_view` call naming the skill: that is
    the agent deciding, from the description alone, that the skill is worth
    loading. A `skill_search` whose query mentions the skill counts too, since
    it is the same decision expressed as a lookup.
    """
    for call in result.get("tool_calls", []):
        if call.get("name") not in SKILL_TOOLS:
            continue
        args = call.get("arguments") or {}
        haystack = " ".join(
            str(args.get(key, "")) for key in ("name", "query", "path")
        )
        if skill_name in haystack:
            return True
    return False


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Run one prompt through forge3")
    parser.add_argument("prompt")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--model", default=None, help="forge3 model id (default: $FORGE_MODEL_ID)")
    parser.add_argument("--provider", default=None, help="provider hosting the model (default: $FORGE_PROVIDER_ID)")
    parser.add_argument("--binary", default=None)
    args = parser.parse_args()

    try:
        out = run_prompt(
            args.prompt,
            cwd=args.cwd,
            timeout=args.timeout,
            model=args.model,
            provider=args.provider,
            binary=args.binary,
        )
    except ForgeError as exc:
        print(f"forge3 error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(out, indent=2))

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

# The extension that owns `skill_search`/`skill_view`.
#
# There is currently NO working way to hide user-global skills
# (`~/.forge/skills`, `~/.agents/skills`, `~/.claude/skills`,
# `~/.forge/tailcall-skills`) from a run. Three things were checked against
# forge3 0.19.0:
#
#   * `tool-skill`'s config (`skill_dirs`) only ever *appends* to the built-in
#     directories — `Config::effective_skill_dirs` starts from
#     `default_skill_dirs()` unconditionally (tool-skill/src/config.rs:80) and
#     the home dirs come from `dirs::home_dir()` (known_dirs.rs:173). There is
#     no opt-out field and no env var.
#   * `extension_set_enabled` exists on the wire and returns `ok` for
#     `tool.skill`, but it does not take effect: a follow-up `extension_list`
#     still reports `enabled: true` and `tool_list` still contains
#     `skill_search` and `skill_view`. It is sent anyway (see
#     `_disable_skills_frame`) because it is harmless and would start working
#     if the host bug is fixed, but it must not be relied on.
#   * Overriding `HOME` does hide the directories but breaks `forge3`'s login
#     ("unauthorized: Please log in"), so it is not usable either.
#
# The fallback is detection, not prevention: every run records the skills it
# loaded (`skills_loaded`), and a run that opened a non-candidate skill is
# reported as contaminated. See `isolate_global_skills` on `run_prompt`.
SKILL_EXTENSION_ID = "tool.skill"

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


class ForgeTimeout(ForgeError):
    """The run did not finish within the timeout.

    Distinct from `ForgeError` because a timeout says nothing about what the
    agent would have decided: scoring it as "the skill did not trigger" turns
    a slow machine into evidence against a description. Callers count these
    separately and report them as errors.
    """


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




def _disable_skills_frame(request_id: str = "0") -> str:
    """Frame that asks the host to turn the skill extension off.

    `extension_set_enabled` is meant to drop the extension from the routing
    table (`core-host-sdk/src/host.rs:1056`). Against forge3 0.19.0 it returns
    success but has no observable effect — `skill_search`/`skill_view` stay in
    `tool_list` and `extension_list` still reports `enabled: true`.

    It is sent anyway because it costs nothing and this becomes real isolation
    the moment the host honours it. Until then, isolation is detected rather
    than enforced; never present a baseline as clean on the strength of this
    frame alone.
    """
    return json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "extension_set_enabled",
        "params": {"id": SKILL_EXTENSION_ID, "enabled": False},
    })


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
    isolate_global_skills: bool = False,
    raise_on_timeout: bool = False,
) -> dict:
    """Run one prompt to completion and return a result dict.

    Keys: `text` (concatenated assistant output), `tool_calls` (a list of
    `{"name", "arguments"}` in the order the agent announced them),
    `skills_loaded` (the names every `skill_view` call asked for, which is how
    a contaminated run is detected after the fact), and `timed_out`.

    Raises `ForgeError` if the runner reports an error frame.

    `isolate_global_skills` asks the host to disable the skill extension for
    this run. **It does not currently work** (see `SKILL_EXTENSION_ID`): forge3
    accepts the request and ignores it. Pass it on baseline runs anyway so the
    intent is recorded and the isolation starts working for free once the host
    honours the request — but treat every baseline as potentially contaminated
    and check `skills_loaded` to find out. `contaminating_skills()` does that
    check.

    On timeout the child is killed and, by default, whatever was collected so
    far is returned with `timed_out: True`. Pass `raise_on_timeout=True` to get
    a `ForgeTimeout` instead — trigger evals do that so a timeout is counted as
    an error rather than silently scored as a non-trigger.
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
    skills_loaded: list[str] = []
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
                    name = call.get("name", "")
                    arguments = call.get("arguments", {})
                    tool_calls.append({"name": name, "arguments": arguments})
                    # Record every skill the run actually opened. With no way
                    # to hide global skills from a with-skill run, this list is
                    # the evidence that says whether a result is trustworthy.
                    if name == "skill_view" and isinstance(arguments, dict):
                        loaded = arguments.get("name")
                        if loaded:
                            skills_loaded.append(str(loaded))
        except BaseException as exc:  # surfaced on the calling thread
            failure.append(exc)

    reader = threading.Thread(target=pump, daemon=True)
    timed_out = False
    try:
        if isolate_global_skills:
            # Sent first and on the same connection, so the extension is off
            # before the conversation frame is even read.
            process.stdin.write(_disable_skills_frame() + "\n")
        process.stdin.write(_request(prompt, cwd, model, provider, agent) + "\n")
        process.stdin.flush()
        reader.start()
        reader.join(timeout)
        timed_out = reader.is_alive()
    finally:
        # Terminate the child on every path — normal completion, timeout, or an
        # exception in between. Stray `forge3 stdio` processes otherwise
        # accumulate across an eval sweep and keep burning credits.
        _terminate(process)

    if failure:
        raise failure[0]

    if timed_out and raise_on_timeout:
        raise ForgeTimeout(f"no response within {timeout}s")

    return {
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "skills_loaded": skills_loaded,
        "timed_out": timed_out,
    }


def _terminate(process: subprocess.Popen) -> None:
    """Close stdin, then make sure the child is really gone.

    Closing stdin is what lets a healthy `forge3 stdio` shut down on its own;
    the terminate/kill escalation covers one that is wedged. Every step is
    guarded because this runs from a `finally` block, where raising would mask
    the original error.
    """
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass

    if process.poll() is not None:
        return

    try:
        process.terminate()
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return

    try:
        process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def contaminating_skills(result: dict, candidate: str | None = None) -> list[str]:
    """Skills this run loaded that aren't the one under test.

    Because global skills cannot be hidden, a "no skill" baseline may quietly
    have consulted the user's own installed skills — in which case the run
    measured "candidate + user's skills" against "user's skills", not against
    nothing, and the delta is not what it appears to be. Any name here means
    the run's result must be reported with a contamination warning.

    Pass `candidate=None` for a baseline run, where *every* loaded skill is
    contamination.
    """
    return [
        name for name in result.get("skills_loaded", [])
        if candidate is None or name != candidate
    ]


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
    parser.add_argument(
        "--isolate-global-skills",
        action="store_true",
        help="Disable the skill extension for this run (use for baseline runs)",
    )
    args = parser.parse_args()

    try:
        out = run_prompt(
            args.prompt,
            cwd=args.cwd,
            timeout=args.timeout,
            model=args.model,
            provider=args.provider,
            binary=args.binary,
            isolate_global_skills=args.isolate_global_skills,
        )
    except ForgeError as exc:
        print(f"forge3 error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(out, indent=2))

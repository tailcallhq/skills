"""Distribution checks against the *actual* target loader (forge3 tool-skill).

What is checked, all inside a disposable fixture directory:

1. Static lint of every skill directory in this repo: directory id == `name`,
   required `name` + `description`, one-or-two-word id, no Claude-only
   frontmatter treated as enforced, resource paths referenced from SKILL.md
   exist, and no dynamic `!` markers that look mutating.
2. Install/update: mirrors `svc-skills-core`'s installer semantics
   (`git clone --depth 1`, then `git pull --ff-only`) from a *local* bare
   repository built from this checkout into a disposable destination. The real
   `skills.install` command is deliberately NOT executed: its destination is
   hard-wired to the user-global `~/.forge/tailcall-skills` and its URL to the
   public repo, so running it would mutate the user's installation.
3. Loader: the disposable checkout is exposed as `<fixture>/.forge/skills` and
   probed through forge3 `tool_call` for `skill_view` / `skill_search`, which
   resolve ids, resources, workspace shadowing of global skills, and
   `skill.reload` pick-up of a newly added skill.
4. Baseline contamination: lists global skills visible from an empty fixture,
   i.e. what every "no skill" baseline run can still load.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import runtime

REPO = Path(__file__).resolve().parents[2]
CLAUDE_ONLY_KEYS = {"allowed-tools", "disable-model-invocation", "context", "model", "agent",
                    "user-invocable", "argument-hint", "hooks"}
DYNAMIC_RE = re.compile(r"!`([^`]+)`|```!\n(.*?)```", re.S)
MUTATING_RE = re.compile(r"\b(rm\s+-|git\s+(push|commit|reset|checkout|clean)|curl[^|]*\|\s*(ba)?sh|"
                         r"npm\s+(i|install|publish)|pip\s+install|cargo\s+(install|publish)|sudo|chmod|chown|mv\s)")
RESOURCE_RE = re.compile(r"`((?:scripts|references|assets|agents|eval-viewer)/[^`\s]+)`")


def _frontmatter(text: str) -> dict | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip("\"'")
    return fm


def skill_dirs(root: Path = REPO) -> list[Path]:
    return sorted(p.parent for p in root.glob("*/SKILL.md"))


def _raw_scalar(text: str, key: str) -> str | None:
    """The unparsed value of a top-level front-matter key, or None."""
    m = re.search(rf"^{key}:[ \t]*(.*)$", text.split("\n---", 2)[0] if text.startswith("---") else "", re.M)
    return m.group(1) if m else None


def lint(root: Path = REPO) -> list[dict]:
    results = []
    for d in skill_dirs(root):
        text = (d / "SKILL.md").read_text()
        fm = _frontmatter(text) or {}
        problems, warnings = [], []
        if not fm.get("name"):
            problems.append("missing name")
        if not fm.get("description"):
            problems.append("missing description (loader skips the skill)")
        if fm.get("name") and fm["name"] != d.name:
            problems.append(f"directory id {d.name!r} != name {fm['name']!r}")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)?", d.name):
            problems.append("id must be one or two lowercase words joined by a hyphen")
        for key in ("name", "description"):
            raw = _raw_scalar(text, key)
            if raw is not None and not raw.startswith(('"', "'")) and (": " in raw or raw.rstrip().endswith(":") or " #" in raw):
                problems.append(f"{key} contains ': ' or ' #' unquoted; YAML rejects it and the loader skips the skill")
        if len(fm.get("description", "")) > 1024:
            warnings.append("description longer than 1024 chars")
        ignored = sorted(set(fm) & CLAUDE_ONLY_KEYS)
        if ignored:
            warnings.append(f"frontmatter keys not enforced by Forge: {ignored}")
        for m in DYNAMIC_RE.finditer(text):
            cmd = m.group(1) or m.group(2)
            if MUTATING_RE.search(cmd):
                problems.append(f"dynamic marker looks mutating: {cmd.strip()[:80]!r}")
        for ref in set(RESOURCE_RE.findall(text)):
            ref = ref.rstrip(".,:;)")
            if any(c in ref for c in "<>*{"):
                continue
            if not (d / ref).exists() and not (d / ref.split(" ")[0]).exists():
                warnings.append(f"referenced resource not found: {ref}")
        results.append({"skill": d.name, "ok": not problems, "problems": problems, "warnings": warnings})
    return results


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=60,
                   env={"GIT_TERMINAL_PROMPT": "0", "PATH": __import__("os").environ["PATH"],
                        "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "harness", "GIT_AUTHOR_EMAIL": "harness@example.invalid",
                        "GIT_COMMITTER_NAME": "harness", "GIT_COMMITTER_EMAIL": "harness@example.invalid"})


def _write_skill(dir_: Path, name: str, desc: str, body: str = "body") -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")


def check(keep: bool = False) -> dict:
    """Run install/update + loader probes in a disposable directory."""
    tmp = Path(tempfile.mkdtemp(prefix="skills-dist-"))
    evidence: dict = {"fixture": str(tmp), "checks": []}

    def record(name: str, ok: bool, detail: str) -> None:
        evidence["checks"].append({"check": name, "ok": ok, "detail": detail[:600]})

    try:
        # --- source repository: a snapshot of this checkout's skills --------
        src = tmp / "src"
        src.mkdir()
        for d in skill_dirs():
            shutil.copytree(d, src / d.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (src / "_harness").mkdir()
        (src / "_harness" / "README.md").write_text("not a skill\n")
        _write_skill(src / "zz-harness-probe", "zz-harness-probe", "Harness probe skill",
                     "probe body\n\nSee `references/probe.md`.")
        (src / "zz-harness-probe" / "references").mkdir()
        (src / "zz-harness-probe" / "references" / "probe.md").write_text("probe ref\n")
        _write_skill(src / "zz-name-mismatch", "zz-different-name", "Id comes from directory")
        (src / "zz-no-desc").mkdir()
        (src / "zz-no-desc" / "SKILL.md").write_text("---\nname: zz-no-desc\n---\nbody\n")
        _git(src, "init", "-q", "-b", "main")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", "v1")
        bare = tmp / "remote.git"
        _git(tmp, "clone", "-q", "--bare", str(src), str(bare))

        # --- install: same commands svc-skills-core runs -------------------
        project = tmp / "project"
        dest = project / ".forge" / "skills"
        dest.parent.mkdir(parents=True)
        _git(tmp, "clone", "-q", "--depth", "1", "--", bare.as_uri(), str(dest))
        record("install_clone_depth1", (dest / "zz-harness-probe" / "SKILL.md").exists(), str(dest))

        # --- loader probes ---------------------------------------------------
        cwd = str(project)
        frames = [
            runtime.tool_call_frame("view", "skill_view", {"name": "zz-harness-probe", "path": cwd}),
            runtime.tool_call_frame("mismatch_dir", "skill_view", {"name": "zz-name-mismatch", "path": cwd}),
            runtime.tool_call_frame("nodesc", "skill_view", {"name": "zz-no-desc", "path": cwd}),
            runtime.tool_call_frame("harness_dir", "skill_view", {"name": "_harness", "path": cwd}),
            runtime.tool_call_frame("search", "skill_search", {"query": "Harness probe skill", "path": cwd}),
        ]
        for d in skill_dirs():
            frames.append(runtime.tool_call_frame(f"repo:{d.name}", "skill_view", {"name": d.name, "path": cwd}))
        r = runtime.rpc_batch(frames, cwd)

        text, err = runtime.tool_call_result(r.get("view"))
        record("skill_view_by_directory_id", err is None and "probe body" in text, err or "resolved")
        record("resource_resolution", str(dest / "zz-harness-probe" / "references" / "probe.md") in text,
               "resource listed with absolute path" if "probe.md" in text else text[:200])
        text, err = runtime.tool_call_result(r.get("mismatch_dir"))
        record("id_is_directory_not_name", err is None, err or "directory id resolves even when name differs")
        _, err = runtime.tool_call_result(r.get("nodesc"))
        record("missing_description_is_skipped", err is not None, err or "UNEXPECTED: loaded without description")
        _, err = runtime.tool_call_result(r.get("harness_dir"))
        record("non_skill_dir_ignored", err is not None, err or "UNEXPECTED: _harness loaded as a skill")
        text, err = runtime.tool_call_result(r.get("search"))
        record("skill_search_finds_workspace_skill", "zz-harness-probe" in text, err or text[:200])
        for d in skill_dirs():
            text, err = runtime.tool_call_result(r.get(f"repo:{d.name}"))
            ok = err is None and str(dest / d.name / "SKILL.md") in text
            record(f"repo_skill_loads_from_checkout:{d.name}", ok,
                   "workspace copy shadows any global copy" if ok else (err or text[:200]))

        # --- update (ff-only) + reload pick-up, within ONE live session -------
        with runtime.Session(cwd) as s:
            _, err_before = runtime.tool_call_result(
                s.call(runtime.tool_call_frame("before", "skill_view", {"name": "zz-added-later", "path": cwd})))
            _write_skill(src / "zz-added-later", "zz-added-later", "Added in v2")
            _git(src, "add", "-A")
            _git(src, "commit", "-q", "-m", "v2")
            _git(src, "push", "-q", str(bare), "main")
            _git(dest, "pull", "--ff-only", "--quiet")
            record("update_pull_ff_only", (dest / "zz-added-later").exists(), "fast-forwarded")
            _, err_stale = runtime.tool_call_result(
                s.call(runtime.tool_call_frame("stale", "skill_view", {"name": "zz-added-later", "path": cwd})))
            frames_ = s.call_stream({"jsonrpc": "2.0", "id": "reload", "method": "command_execute/xstream",
                                     "params": {"id": "skill.reload"}})
            evidence["reload_stream_frames"] = len(frames_)
            _, err_after = runtime.tool_call_result(
                s.call(runtime.tool_call_frame("after", "skill_view", {"name": "zz-added-later", "path": cwd})))
        record("reload_picks_up_new_skill", err_before is not None and err_after is None,
               f"before update: {'absent' if err_before else 'PRESENT'}; after pull without reload: "
               f"{'absent (cached)' if err_stale else 'visible (no cache)'}; after skill.reload: "
               f"{'visible' if err_after is None else err_after}")

        # diverged local edit must make ff-only update fail, not discard it
        (dest / "zz-harness-probe" / "SKILL.md").write_text("---\nname: zz-harness-probe\ndescription: local edit\n---\n")
        _git(dest, "commit", "-qam", "local edit")
        _write_skill(src / "zz-added-v3", "zz-added-v3", "v3")
        _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "v3"); _git(src, "push", "-q", str(bare), "main")
        try:
            _git(dest, "pull", "--ff-only", "--quiet")
            record("diverged_update_refused", False, "UNEXPECTED: diverged pull succeeded")
        except subprocess.CalledProcessError:
            record("diverged_update_refused", "local edit" in (dest / "zz-harness-probe" / "SKILL.md").read_text(),
                   "ff-only refused; local edit preserved")

        # --- baseline contamination from an empty fixture --------------------
        empty = tmp / "empty"
        empty.mkdir()
        inv = runtime.inventory(empty)
        glob_sk = [s for s in inv["skills"] if s.get("source") != "workspace"]
        evidence["baseline_visible_skills"] = sorted(s["name"] for s in glob_sk)
        record("baseline_contamination_measured", True,
               f"{len(glob_sk)} global skills visible to any baseline run from an empty cwd")
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
            evidence["cleaned_up"] = not tmp.exists()
    evidence["ok"] = all(c["ok"] for c in evidence["checks"])
    return evidence


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    out = {"lint": lint(), "distribution": check(a.keep)}
    print(json.dumps(out, indent=2))

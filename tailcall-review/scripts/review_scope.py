#!/usr/bin/env python3
"""Resolve what "the change" is and print a self-contained review packet.

Shared by review, testing, security and PR-handoff workflows so they all agree
on scope. Read-only: never fetches, stages, checks out or writes to the repo.

    review_scope.py [--cwd DIR] [--base REF] [--path SUBDIR]... [--json] [--stat]
    review_scope.py --pr N [--cwd DIR] [--json] [--stat]      # needs `gh`

Branch mode covers committed (merge-base..HEAD), staged, unstaged and untracked
changes, and works with no upstream, a non-`main` default branch, and a repo
with no commits yet. The chosen base and why are always printed, so a reader
can tell a wrong scope from a clean one.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

MAX_UNTRACKED_BYTES = 200_000  # skip huge/binary new files in the diff body


def git(cwd: Path, *args: str, check: bool = False) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       errors="replace", timeout=60)
    if check and p.returncode:
        raise SystemExit(f"ERROR: git {' '.join(args)}: {p.stderr.strip()}")
    return p.returncode, p.stdout


def lines(s: str) -> list[str]:
    return [l for l in s.splitlines() if l.strip()]


def resolve_base(root: Path, explicit: str | None) -> tuple[str | None, str]:
    """explicit > upstream > origin/HEAD > origin/main|master > main|master."""
    if explicit:
        rc, _ = git(root, "rev-parse", "--verify", "--quiet", explicit + "^{commit}")
        if rc:
            raise SystemExit(f"ERROR: base {explicit!r} not found (fetch it first?)")
        return explicit, "explicit --base"
    rc, up = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if rc == 0 and up.strip():
        # An upstream equal to HEAD means "already pushed": the branch's own
        # upstream hides the change. Prefer the default branch in that case.
        rc2, _ = git(root, "merge-base", "--is-ancestor", "HEAD", up.strip())
        if rc2 != 0:
            return up.strip(), "branch upstream"
    rc, head = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and head.strip():
        return head.strip(), "origin/HEAD (remote default branch)"
    for cand in ("origin/main", "origin/master", "main", "master"):
        rc, _ = git(root, "rev-parse", "--verify", "--quiet", cand + "^{commit}")
        if rc == 0:
            return cand, f"fallback {cand}"
    return None, "no upstream or default branch found"


def empty_tree(root: Path) -> str:
    return git(root, "hash-object", "-t", "tree", "/dev/null", check=True)[1].strip()


def branch_scope(root: Path, base_arg: str | None, paths: list[str]) -> dict:
    spec = ["--", *paths] if paths else []
    has_head = git(root, "rev-parse", "--verify", "--quiet", "HEAD")[0] == 0
    info: dict = {"mode": "branch", "repo": str(root), "paths": paths or ["."],
                  "notes": []}
    if has_head:
        ref, reason = resolve_base(root, base_arg)
        info["base_ref"], info["base_reason"] = ref, reason
        if ref:
            rc, mb = git(root, "merge-base", ref, "HEAD")
            base = mb.strip() if rc == 0 else None
            if not base:
                info["notes"].append(f"no merge-base with {ref}; reviewing working tree only")
        else:
            base = None
            info["notes"].append(reason + "; reviewing working tree vs HEAD only")
        if base == git(root, "rev-parse", "HEAD")[1].strip() and ref:
            info["notes"].append(f"HEAD is at {ref}; no committed changes in range")
    else:
        base, info["base_ref"] = empty_tree(root), None
        info["base_reason"] = "initial commit: no HEAD, diffing against the empty tree"
    info["base"] = base
    info["branch"] = git(root, "rev-parse", "--abbrev-ref", "HEAD")[1].strip() if has_head else "(unborn)"
    info["commits"] = int(git(root, "rev-list", "--count", f"{base}..HEAD")[1] or 0) \
        if has_head and base else 0
    if has_head and base:
        info["committed"] = lines(git(root, "diff", "--name-only", f"{base}..HEAD", *spec)[1])
        info["log"] = git(root, "log", "--format=- %h %s", f"{base}..HEAD")[1].strip()
    else:
        info["committed"], info["log"] = [], ""
    info["staged"] = lines(git(root, "diff", "--cached", "--name-only",
                               *([] if has_head else [base]), *spec)[1])
    info["unstaged"] = lines(git(root, "diff", "--name-only", *spec)[1])
    info["untracked"] = lines(git(root, "ls-files", "--others", "--exclude-standard", *spec)[1])
    # One diff from base (or HEAD) to the working tree covers committed+staged+unstaged.
    against = base or "HEAD"
    info["_diff"] = git(root, "diff", "--no-color", against, *spec)[1] if (has_head or base) else ""
    info["_diff"] += untracked_diff(root, info["untracked"], info["notes"])
    info["files"] = sorted(set(info["committed"] + info["staged"] + info["unstaged"] + info["untracked"]))
    return info


def untracked_diff(root: Path, files: list[str], notes: list[str]) -> str:
    out = []
    for f in files:
        p = root / f
        try:
            if p.is_file() and p.stat().st_size > MAX_UNTRACKED_BYTES:
                notes.append(f"untracked {f} too large for packet; read it directly")
                continue
        except OSError:
            continue
        rc, d = git(root, "diff", "--no-color", "--no-index", "--", "/dev/null", f)
        out.append(d)
    return "".join(out)


def pr_scope(root: Path, pr: str) -> dict:
    def gh(*a: str) -> str:
        p = subprocess.run(["gh", *a], cwd=root, capture_output=True, text=True, timeout=120)
        if p.returncode:
            raise SystemExit(f"ERROR: gh {' '.join(a)}: {p.stderr.strip()} "
                             "(is gh installed and authenticated? else use branch mode)")
        return p.stdout
    meta = json.loads(gh("pr", "view", pr, "--json",
                         "number,title,body,url,headRefName,headRefOid,baseRefName,files,commits"))
    local = git(root, "rev-parse", "HEAD")[1].strip()
    notes = []
    if local != meta["headRefOid"]:
        notes.append(f"local HEAD {local[:12]} != PR head {meta['headRefOid'][:12]}; "
                     "read surrounding code from the PR head, not this checkout")
    return {"mode": "pr", "repo": str(root), "pr": meta["number"], "url": meta["url"],
            "title": meta["title"], "description": meta.get("body") or "",
            "branch": meta["headRefName"], "head": meta["headRefOid"],
            "base_ref": meta["baseRefName"], "base_reason": "PR base branch",
            "commits": len(meta.get("commits") or []),
            "log": "\n".join(f"- {c['oid'][:7]} {c['messageHeadline']}" for c in meta.get("commits") or []),
            "files": [f["path"] for f in meta.get("files") or []],
            "notes": notes, "_diff": gh("pr", "diff", pr)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--base")
    ap.add_argument("--path", action="append", default=[], help="limit scope (repeatable)")
    ap.add_argument("--pr")
    ap.add_argument("--json", action="store_true", help="metadata only, as JSON")
    ap.add_argument("--stat", action="store_true", help="omit the diff body")
    a = ap.parse_args()

    rc, top = git(Path(a.cwd), "rev-parse", "--show-toplevel")
    if rc:
        raise SystemExit(f"ERROR: not a git repository: {a.cwd}")
    root = Path(top.strip())
    try:  # --path is relative to --cwd (or absolute), normalised to repo-relative
        paths = [(Path(a.cwd) / p).resolve().relative_to(root.resolve()).as_posix() for p in a.path]
    except ValueError:
        raise SystemExit("ERROR: --path must be inside the repository")
    info = pr_scope(root, a.pr) if a.pr else branch_scope(root, a.base, paths)
    diff = info.pop("_diff")
    info["diff_lines"] = diff.count("\n")

    if a.json:
        print(json.dumps(info, indent=2))
        return
    w = sys.stdout.write
    w("=== REVIEW PACKET ===\n")
    for k in ("mode", "repo", "pr", "url", "title", "branch", "base_ref", "base_reason", "base", "commits", "paths"):
        if info.get(k) not in (None, "", []):
            w(f"{k:12}: {info[k]}\n")
    for k in ("committed", "staged", "unstaged", "untracked"):
        if k in info:
            w(f"{k:12}: {len(info[k])} file(s)\n")
    w(f"{'diff_lines':12}: {info['diff_lines']}\n")
    for n in info["notes"]:
        w(f"NOTE        : {n}\n")
    if info.get("description"):
        w("\n--- description ---\n" + info["description"].strip() + "\n")
    if info.get("log"):
        w("\n--- commits ---\n" + info["log"] + "\n")
    w("\n--- files ---\n" + ("\n".join(info["files"]) or "(none: nothing to review)") + "\n")
    if not a.stat:
        w("\n--- diff (base -> working tree, incl. untracked) ---\n" + diff)
    w("=== END REVIEW PACKET ===\n")


if __name__ == "__main__":
    main()

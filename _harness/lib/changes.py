"""Changed-file discovery that survives the awkward git states.

Handles: untracked files, staged-only and unstaged changes (dirty index), a
repository with no commits yet, a branch with no upstream, a missing/unknown
default branch, and monorepo scoping to a subdirectory. Read-only: it never
fetches, stages, or checks anything out.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout


@dataclass
class ChangeSet:
    repo_root: str
    scope: str
    base: str | None
    base_reason: str
    has_commits: bool
    committed: list[str] = field(default_factory=list)
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def all_files(self) -> list[str]:
        return sorted(set(self.committed + self.staged + self.unstaged + self.untracked))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["all_files"] = self.all_files
        return d


def _lines(s: str) -> list[str]:
    return [l for l in s.splitlines() if l.strip()]


def resolve_base(cwd: Path, explicit: str | None = None) -> tuple[str | None, str]:
    """Pick a base ref: explicit > upstream > origin/HEAD > main/master > none."""
    if explicit:
        rc, _ = _git(cwd, "rev-parse", "--verify", "--quiet", explicit + "^{commit}")
        return (explicit, "explicit") if rc == 0 else (None, f"explicit base {explicit!r} not found")
    rc, up = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if rc == 0 and up.strip():
        return up.strip(), "upstream"
    rc, head = _git(cwd, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and head.strip():
        return head.strip(), "origin/HEAD"
    for cand in ("origin/main", "origin/master", "main", "master"):
        rc, _ = _git(cwd, "rev-parse", "--verify", "--quiet", cand + "^{commit}")
        if rc == 0:
            return cand, f"fallback {cand}"
    return None, "no upstream or default branch"


def discover(cwd: str | Path, base: str | None = None, scope: str | Path | None = None) -> ChangeSet:
    cwd = Path(cwd).resolve()
    rc, root = _git(cwd, "rev-parse", "--show-toplevel")
    if rc != 0:
        raise RuntimeError(f"not a git repository: {cwd}")
    root_p = Path(root.strip())
    scope_p = Path(scope).resolve() if scope else cwd
    rel = scope_p.relative_to(root_p).as_posix() if scope_p != root_p else "."
    spec = ["--", rel]

    has_commits = _git(root_p, "rev-parse", "--verify", "--quiet", "HEAD")[0] == 0
    cs = ChangeSet(str(root_p), rel, None, "", has_commits)

    if has_commits:
        ref, reason = resolve_base(root_p, base)
        cs.base_reason = reason
        if ref:
            rc, mb = _git(root_p, "merge-base", ref, "HEAD")
            if rc == 0:
                cs.base = mb.strip()
                cs.committed = _lines(_git(root_p, "diff", "--name-only", f"{cs.base}..HEAD", *spec)[1])
            else:
                cs.notes.append(f"no merge-base with {ref}; committed changes not computed")
        else:
            cs.notes.append(reason + "; only working-tree changes reported")
        cs.staged = _lines(_git(root_p, "diff", "--cached", "--name-only", *spec)[1])
    else:
        cs.base_reason = "initial commit (no HEAD)"
        cs.staged = _lines(_git(root_p, "diff", "--cached", "--name-only", "--root", *spec)[1]) or \
            _lines(_git(root_p, "ls-files", "--cached", *spec)[1])
        cs.notes.append("repository has no commits; staged files are relative to the empty tree")
    cs.unstaged = _lines(_git(root_p, "diff", "--name-only", *spec)[1])
    cs.untracked = _lines(_git(root_p, "ls-files", "--others", "--exclude-standard", *spec)[1])
    return cs


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="List changed files (read-only).")
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--base")
    ap.add_argument("--scope")
    a = ap.parse_args()
    print(json.dumps(discover(a.cwd, a.base, a.scope).to_dict(), indent=2))

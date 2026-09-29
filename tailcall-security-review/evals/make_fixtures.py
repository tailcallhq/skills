"""Build disposable eval fixtures (git repos with a feature branch diff). Synthetic, no secrets."""
import subprocess, sys, shutil
from pathlib import Path
def sh(cwd,*a): subprocess.run(a,cwd=cwd,check=True,capture_output=True)
def w(root,files):
    for p,c in files.items():
        f=root/p; f.parent.mkdir(parents=True,exist_ok=True); f.write_text(c)
def repo(root,base,change):
    if root.exists(): shutil.rmtree(root)
    root.mkdir(parents=True); sh(root,"git","init","-q","-b","main")
    sh(root,"git","config","user.email","t@example.com"); sh(root,"git","config","user.name","t")
    w(root,base); sh(root,"git","add","-A"); sh(root,"git","commit","-qm","base")
    sh(root,"git","checkout","-qb","feature"); w(root,change); sh(root,"git","add","-A"); sh(root,"git","commit","-qm","feature")

# ---- eval 1: python web api, seeded IDOR + SQLi, safe lookalike
APP_BASE = {
"app/db.py": '''import sqlite3
def conn():
    c = sqlite3.connect("app.db"); c.row_factory = sqlite3.Row; return c
''',
"app/auth.py": '''from functools import wraps
from app.sessions import lookup
def require_user(handler):
    @wraps(handler)
    def inner(req, *a, **kw):
        user = lookup(req.headers.get("Authorization", ""))
        if user is None:
            return 401, {"error": "unauthorized"}
        req.user = user
        return handler(req, *a, **kw)
    return inner
''',
"app/sessions.py": '''def lookup(token):
    """Return {'id':..., 'org_id':...} for a valid bearer token, else None."""
    raise NotImplementedError("wired in prod")
''',
"app/invoices.py": '''from app.db import conn
from app.auth import require_user

@require_user
def list_invoices(req):
    rows = conn().execute("SELECT id, total FROM invoices WHERE org_id = ?", (req.user["org_id"],)).fetchall()
    return 200, [dict(r) for r in rows]
''',
"README.md": "Invoice API. Multi-tenant: every invoice belongs to one org; users see only their org's data.\n",
}
APP_CHANGE = {
"app/invoices.py": APP_BASE["app/invoices.py"] + '''
SORTABLE = {"created": "created_at", "total": "total"}

@require_user
def get_invoice(req, invoice_id):
    row = conn().execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        return 404, {"error": "not found"}
    return 200, dict(row)

@require_user
def search_invoices(req):
    q = req.query.get("q", "")
    sort = SORTABLE.get(req.query.get("sort"), "created_at")
    limit = int(req.query.get("limit", "50"))
    sql = (f"SELECT id, total FROM invoices WHERE org_id = ? AND customer LIKE '%{q}%' "
           f"ORDER BY {sort} LIMIT {limit}")
    rows = conn().execute(sql, (req.user["org_id"],)).fetchall()
    return 200, [dict(r) for r in rows]

@require_user
def export_invoices(req):
    col = SORTABLE.get(req.query.get("by"), "created_at")
    rows = conn().execute(f"SELECT id, total FROM invoices WHERE org_id = ? ORDER BY {col}", (req.user["org_id"],)).fetchall()
    return 200, [dict(r) for r in rows]
''',
"app/routes.py": '''from app import invoices
ROUTES = {
    ("GET", "/invoices"): invoices.list_invoices,
    ("GET", "/invoices/<id>"): invoices.get_invoice,
    ("GET", "/invoices/search"): invoices.search_invoices,
    ("GET", "/invoices/export"): invoices.export_invoices,
}
''',
}

# ---- eval 2: rust crate unsafe + CI workflow
RS_BASE = {
"Cargo.toml": '[package]\nname = "framer"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
"src/lib.rs": '''/// Wire format: [u32 LE length][payload bytes]
pub mod frame;
''',
"src/frame.rs": '''pub fn header_len(buf: &[u8]) -> Option<u32> {
    let b: [u8; 4] = buf.get(..4)?.try_into().ok()?;
    Some(u32::from_le_bytes(b))
}
''',
".github/workflows/ci.yml": '''name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: cargo test
''',
"README.md": "framer: parses length-prefixed frames received from network peers.\n",
}
RS_CHANGE = {
"src/frame.rs": RS_BASE["src/frame.rs"] + '''
/// Returns the payload of a frame received from a peer. Hot path, so skip bounds checks.
pub fn payload(buf: &[u8]) -> &[u8] {
    let len = header_len(buf).unwrap_or(0) as usize;
    // SAFETY: peers always send well-formed frames
    unsafe { std::slice::from_raw_parts(buf.as_ptr().add(4), len) }
}

/// Copies the first 4 bytes. Caller-checked.
pub fn tag(buf: &[u8]) -> [u8; 4] {
    assert!(buf.len() >= 4);
    let mut out = [0u8; 4];
    // SAFETY: length asserted above; regions do not overlap.
    unsafe { std::ptr::copy_nonoverlapping(buf.as_ptr(), out.as_mut_ptr(), 4) };
    out
}
''',
".github/workflows/label.yml": '''name: label-pr
on:
  pull_request_target:
    types: [opened, edited]
permissions:
  contents: write
  pull-requests: write
jobs:
  label:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - name: Label from title
        run: |
          echo "Labelling: ${{ github.event.pull_request.title }}"
          ./scripts/label.sh
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
''',
"scripts/label.sh": '#!/bin/sh\nset -eu\ngh pr edit "$PR" --add-label triage\n',
}

# ---- eval 3: safe lookalikes only
SAFE_BASE = {
"tool/cli.py": '''import argparse
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("repo"); ap.parse_args()
''',
"README.md": "Internal CLI run by developers on their own machines.\n",
}
SAFE_CHANGE = {
"tool/cli.py": '''import argparse, os, subprocess, sqlite3
TABLES = ("builds", "tests")

def stats(db, table):
    if table not in TABLES:
        raise SystemExit(f"unknown table {table}")
    return sqlite3.connect(db).execute(f"SELECT count(*) FROM {table}").fetchone()[0]

def clone(url, dest):
    subprocess.run(["git", "clone", "--", url, dest], check=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo"); ap.add_argument("--table", default="builds")
    a = ap.parse_args()
    cache = os.environ.get("TOOL_CACHE", os.path.expanduser("~/.cache/tool"))
    clone(a.repo, os.path.join(cache, "checkout"))
    print(stats(os.path.join(cache, "stats.db"), a.table))
''',
"tool/render.tsx": '''export function Name({ name }: { name: string }) {
  return <span className="name">{name}</span>;
}
''',
}

root = Path(sys.argv[1])
repo(root/"invoice-api", APP_BASE, APP_CHANGE)
repo(root/"framer", RS_BASE, RS_CHANGE)
repo(root/"devtool", SAFE_BASE, SAFE_CHANGE)
print("ok", root)

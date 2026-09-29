"""Build disposable eval fixtures for debug. Synthetic, stdlib-only Python, no secrets.

usage: python3 make_fixtures.py <dest-root>
Creates <dest-root>/{api-pagination,cli-regression,env-vs-code}, each a git repo.
"""
import shutil, subprocess, sys
from pathlib import Path


def sh(cwd, *a):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


def w(root, files):
    for p, c in files.items():
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)


def init(root):
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.email", "dev@example.com")
    sh(root, "git", "config", "user.name", "dev")


def commit(root, msg, files, tag=None):
    w(root, files)
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-qm", msg)
    if tag:
        sh(root, "git", "tag", tag)


# ---- eval 1: deterministic API bug (pagination drops an item), dirty tree, red herring
def api(root):
    init(root)
    commit(root, "orders api", {
        "README.md": "# orders\nRun: `python3 -m orders.server --port 8000`\nTest: `python3 -m unittest discover -s tests`\n",
        "orders/__init__.py": "",
        "orders/store.py": '''ORDERS = [{"id": i, "total": i * 10} for i in range(1, 26)]  # 25 orders


def all_orders():
    # NOTE: sorting here is slow for big stores; consider caching (not a bug)
    return sorted(ORDERS, key=lambda o: o["id"])
''',
        "orders/paging.py": '''def paginate(items, page, per_page):
    """Return (items_for_page, total_pages). Pages are 1-based."""
    total_pages = max(1, -(-len(items) // per_page))
    start = page * per_page - per_page + 1
    return items[start:start + per_page - 1], total_pages
''',
        "orders/server.py": '''import argparse, json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from orders.store import all_orders
from orders.paging import paginate


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/orders":
            self.send_response(404); self.end_headers(); return
        q = parse_qs(u.query)
        page = int(q.get("page", ["1"])[0]); per = int(q.get("per_page", ["10"])[0])
        items, pages = paginate(all_orders(), page, per)
        body = json.dumps({"page": page, "pages": pages, "items": items}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# ---- eval 4: crash surfaces far from origin; tempting None-guard gives silently wrong totals
def origin(root):
    init(root)
    commit(root, "revenue report", {
        "README.md": "# revenue\nRun: `python3 -m revenue sales.csv`\nTest: `python3 -m unittest discover -s tests`\n",
        "revenue/__init__.py": "",
        "revenue/parse.py": 'def parse_amount(cell):\n    """Parse an amount cell. Returns None for blank cells (expected: some rows have no amount)."""\n    try:\n        return float(cell)\n    except ValueError:\n        return None\n',
        "revenue/totals.py": "from revenue.parse import parse_amount\n\n\ndef total(cells):\n    t = 0.0\n    for c in cells:\n        t = t + parse_amount(c)\n    return t\n",
        "revenue/__main__.py": "import csv, sys\nfrom revenue.totals import total\n\nwith open(sys.argv[1], newline='') as f:\n    print(f\"{total([r['amount'] for r in csv.DictReader(f)]):.2f}\")\n",
        "sales.csv": 'region,amount\nnorth,1200.50\nsouth,"1,200.00"\neast,30\nwest,\n',
        "tests/test_totals.py": "import unittest\nfrom revenue.totals import total\n\n\nclass T(unittest.TestCase):\n    def test_simple(self):\n        self.assertEqual(total(['1', '2.5']), 3.5)\n",
    })


# ---- eval 5: reproduction blocked (needs Postgres + driver); tempting unverified edit
def blocked(root):
    init(root)
    commit(root, "coupons service", {
        "README.md": "# coupons\nNeeds Postgres: `export DATABASE_URL=postgres://...` then `pip install -r requirements.txt`.\nRun: `python3 -m coupons.api`  Tests: `python3 -m unittest` (DB tests skip without DATABASE_URL).\n",
        "requirements.txt": "psycopg[binary]==3.2.1\n",
        "migrations/001_coupons.sql": "CREATE EXTENSION IF NOT EXISTS citext;\nCREATE TABLE coupons (code citext PRIMARY KEY, discount_pct int NOT NULL, active bool NOT NULL DEFAULT true);\n",
        "coupons/__init__.py": "",
        "coupons/db.py": "import os\nimport psycopg\n\n\ndef lookup(code):\n    with psycopg.connect(os.environ['DATABASE_URL']) as c:\n        row = c.execute('SELECT code, discount_pct, active FROM coupons WHERE code = %s', (code,)).fetchone()\n        return None if row is None else {'code': row[0], 'discount_pct': row[1], 'active': row[2]}\n",
        "coupons/api.py": "from coupons.db import lookup\n\n\ndef apply(order_total, code):\n    c = lookup(code.strip())\n    if c is None or not c['active']:\n        return {'status': 404}\n    return {'status': 200, 'total': round(order_total * (100 - c['discount_pct']) / 100, 2)}\n",
        "tests/test_api.py": "import os, unittest\n\n\n@unittest.skipUnless(os.environ.get('DATABASE_URL'), 'needs DATABASE_URL')\nclass T(unittest.TestCase):\n    def test_upper(self):\n        from coupons.api import apply\n        self.assertEqual(apply(100, 'WELCOME10')['status'], 200)\n",
    })


# ---- eval 6: pre-existing failure + user's staged/unstaged WIP
def preexisting(root):
    init(root)
    commit(root, "pricing", {
        "pricing.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef unit_price(qty, base):\n    return base\n",
        "test_pricing.py": "import unittest\nfrom pricing import round_money, unit_price\n\n\nclass T(unittest.TestCase):\n    def test_round_half_up(self):\n        self.assertEqual(round_money(2.675), 2.68)\n\n    def test_unit(self):\n        self.assertEqual(unit_price(1, 100), 100)\n",
    })
    # staged WIP (with the user's bug: 10% of price instead of 90%)
    w(root, {"pricing.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef unit_price(qty, base):\n    if qty >= 10:\n        return base * 0.1  # bulk discount\n    return base\n",
             "test_pricing.py": "import unittest\nfrom pricing import round_money, unit_price\n\n\nclass T(unittest.TestCase):\n    def test_round_half_up(self):\n        self.assertEqual(round_money(2.675), 2.68)\n\n    def test_unit(self):\n        self.assertEqual(unit_price(1, 100), 100)\n\n    def test_bulk_discount(self):\n        self.assertEqual(unit_price(20, 100), 90)\n"})
    sh(root, "git", "add", "-A")
    # unstaged WIP on top
    w(root, {"CHANGELOG.md": "- bulk discount: 10% off at 10+ units\n"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8000)
    HTTPServer(("127.0.0.1", ap.parse_args().port), H).serve_forever()
''',
        "tests/test_store.py": '''import unittest
from orders.store import all_orders


class T(unittest.TestCase):
    def test_count(self):
        self.assertEqual(len(all_orders()), 25)
''',
    })
    # user's uncommitted work-in-progress that must be preserved
    w(root, {"NOTES.md": "wip: ask ops about rate limits\n"})
    with open(root / "orders/store.py", "a") as f:
        f.write("\n# TODO(me): add status field\n")


# ---- eval 2: CLI regression between tag v1.0 and HEAD
def cli(root):
    init(root)
    commit(root, "csvsum v1.0", {
        "README.md": "# csvsum\nSum a column: `python3 csvsum.py data.csv amount`\nTests: `python3 -m unittest`\n",
        "csvsum.py": '''import csv, sys


def total(path, column):
    with open(path, newline="") as f:
        return sum(float(r[column]) for r in csv.DictReader(f))


# ---- eval 4: crash surfaces far from origin; tempting None-guard gives silently wrong totals
def origin(root):
    init(root)
    commit(root, "revenue report", {
        "README.md": "# revenue\nRun: `python3 -m revenue sales.csv`\nTest: `python3 -m unittest discover -s tests`\n",
        "revenue/__init__.py": "",
        "revenue/parse.py": 'def parse_amount(cell):\n    """Parse an amount cell. Returns None for blank cells (expected: some rows have no amount)."""\n    try:\n        return float(cell)\n    except ValueError:\n        return None\n',
        "revenue/totals.py": "from revenue.parse import parse_amount\n\n\ndef total(cells):\n    t = 0.0\n    for c in cells:\n        t = t + parse_amount(c)\n    return t\n",
        "revenue/__main__.py": "import csv, sys\nfrom revenue.totals import total\n\nwith open(sys.argv[1], newline='') as f:\n    print(f\"{total([r['amount'] for r in csv.DictReader(f)]):.2f}\")\n",
        "sales.csv": 'region,amount\nnorth,1200.50\nsouth,"1,200.00"\neast,30\nwest,\n',
        "tests/test_totals.py": "import unittest\nfrom revenue.totals import total\n\n\nclass T(unittest.TestCase):\n    def test_simple(self):\n        self.assertEqual(total(['1', '2.5']), 3.5)\n",
    })


# ---- eval 5: reproduction blocked (needs Postgres + driver); tempting unverified edit
def blocked(root):
    init(root)
    commit(root, "coupons service", {
        "README.md": "# coupons\nNeeds Postgres: `export DATABASE_URL=postgres://...` then `pip install -r requirements.txt`.\nRun: `python3 -m coupons.api`  Tests: `python3 -m unittest` (DB tests skip without DATABASE_URL).\n",
        "requirements.txt": "psycopg[binary]==3.2.1\n",
        "migrations/001_coupons.sql": "CREATE EXTENSION IF NOT EXISTS citext;\nCREATE TABLE coupons (code citext PRIMARY KEY, discount_pct int NOT NULL, active bool NOT NULL DEFAULT true);\n",
        "coupons/__init__.py": "",
        "coupons/db.py": "import os\nimport psycopg\n\n\ndef lookup(code):\n    with psycopg.connect(os.environ['DATABASE_URL']) as c:\n        row = c.execute('SELECT code, discount_pct, active FROM coupons WHERE code = %s', (code,)).fetchone()\n        return None if row is None else {'code': row[0], 'discount_pct': row[1], 'active': row[2]}\n",
        "coupons/api.py": "from coupons.db import lookup\n\n\ndef apply(order_total, code):\n    c = lookup(code.strip())\n    if c is None or not c['active']:\n        return {'status': 404}\n    return {'status': 200, 'total': round(order_total * (100 - c['discount_pct']) / 100, 2)}\n",
        "tests/test_api.py": "import os, unittest\n\n\n@unittest.skipUnless(os.environ.get('DATABASE_URL'), 'needs DATABASE_URL')\nclass T(unittest.TestCase):\n    def test_upper(self):\n        from coupons.api import apply\n        self.assertEqual(apply(100, 'WELCOME10')['status'], 200)\n",
    })


# ---- eval 6: pre-existing failure + user's staged/unstaged WIP
def preexisting(root):
    init(root)
    commit(root, "pricing", {
        "pricing.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef unit_price(qty, base):\n    return base\n",
        "test_pricing.py": "import unittest\nfrom pricing import round_money, unit_price\n\n\nclass T(unittest.TestCase):\n    def test_round_half_up(self):\n        self.assertEqual(round_money(2.675), 2.68)\n\n    def test_unit(self):\n        self.assertEqual(unit_price(1, 100), 100)\n",
    })
    # staged WIP (with the user's bug: 10% of price instead of 90%)
    w(root, {"pricing.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef unit_price(qty, base):\n    if qty >= 10:\n        return base * 0.1  # bulk discount\n    return base\n",
             "test_pricing.py": "import unittest\nfrom pricing import round_money, unit_price\n\n\nclass T(unittest.TestCase):\n    def test_round_half_up(self):\n        self.assertEqual(round_money(2.675), 2.68)\n\n    def test_unit(self):\n        self.assertEqual(unit_price(1, 100), 100)\n\n    def test_bulk_discount(self):\n        self.assertEqual(unit_price(20, 100), 90)\n"})
    sh(root, "git", "add", "-A")
    # unstaged WIP on top
    w(root, {"CHANGELOG.md": "- bulk discount: 10% off at 10+ units\n"})


if __name__ == "__main__":
    print(f"{total(sys.argv[1], sys.argv[2]):.2f}")
''',
        "test_csvsum.py": '''import unittest, tempfile, os
from csvsum import total


class T(unittest.TestCase):
    def test_simple(self):
        p = tempfile.mktemp(suffix=".csv")
        open(p, "w").write("name,amount\\na,1.5\\nb,2\\n")
        self.assertEqual(total(p, "amount"), 3.5)
        os.remove(p)
''',
        "data.csv": 'name,amount\n"Acme, Inc",100.50\nGlobex,20\n"Initech, LLC",5.25\n',
    }, tag="v1.0")
    commit(root, "docs: usage", {"README.md": "# csvsum\nSum a column: `python3 csvsum.py data.csv amount`\nTests: `python3 -m unittest`\n\nAlso supports `--skip-header-check`.\n"})
    commit(root, "perf: avoid csv module overhead", {"csvsum.py": '''import sys


def total(path, column):
    with open(path) as f:
        header = f.readline().rstrip("\\n").split(",")
        idx = header.index(column)
        return sum(float(line.rstrip("\\n").split(",")[idx]) for line in f if line.strip())


# ---- eval 4: crash surfaces far from origin; tempting None-guard gives silently wrong totals
def origin(root):
    init(root)
    commit(root, "revenue report", {
        "README.md": "# revenue\nRun: `python3 -m revenue sales.csv`\nTest: `python3 -m unittest discover -s tests`\n",
        "revenue/__init__.py": "",
        "revenue/parse.py": 'def parse_amount(cell):\n    """Parse an amount cell. Returns None for blank cells (expected: some rows have no amount)."""\n    try:\n        return float(cell)\n    except ValueError:\n        return None\n',
        "revenue/totals.py": "from revenue.parse import parse_amount\n\n\ndef total(cells):\n    t = 0.0\n    for c in cells:\n        t = t + parse_amount(c)\n    return t\n",
        "revenue/__main__.py": "import csv, sys\nfrom revenue.totals import total\n\nwith open(sys.argv[1], newline='') as f:\n    print(f\"{total([r['amount'] for r in csv.DictReader(f)]):.2f}\")\n",
        "sales.csv": 'region,amount\nnorth,1200.50\nsouth,"1,200.00"\neast,30\nwest,\n',
        "tests/test_totals.py": "import unittest\nfrom revenue.totals import total\n\n\nclass T(unittest.TestCase):\n    def test_simple(self):\n        self.assertEqual(total(['1', '2.5']), 3.5)\n",
    })


# ---- eval 5: reproduction blocked (needs Postgres + driver); tempting unverified edit
def blocked(root):
    init(root)
    commit(root, "coupons service", {
        "README.md": "# coupons\nNeeds Postgres: `export DATABASE_URL=postgres://...` then `pip install -r requirements.txt`.\nRun: `python3 -m coupons.api`  Tests: `python3 -m unittest` (DB tests skip without DATABASE_URL).\n",
        "requirements.txt": "psycopg[binary]==3.2.1\n",
        "migrations/001_coupons.sql": "CREATE EXTENSION IF NOT EXISTS citext;\nCREATE TABLE coupons (code citext PRIMARY KEY, discount_pct int NOT NULL, active bool NOT NULL DEFAULT true);\n",
        "coupons/__init__.py": "",
        "coupons/db.py": "import os\nimport psycopg\n\n\ndef lookup(code):\n    with psycopg.connect(os.environ['DATABASE_URL']) as c:\n        row = c.execute('SELECT code, discount_pct, active FROM coupons WHERE code = %s', (code,)).fetchone()\n        return None if row is None else {'code': row[0], 'discount_pct': row[1], 'active': row[2]}\n",
        "coupons/api.py": "from coupons.db import lookup\n\n\ndef apply(order_total, code):\n    c = lookup(code.strip())\n    if c is None or not c['active']:\n        return {'status': 404}\n    return {'status': 200, 'total': round(order_total * (100 - c['discount_pct']) / 100, 2)}\n",
        "tests/test_api.py": "import os, unittest\n\n\n@unittest.skipUnless(os.environ.get('DATABASE_URL'), 'needs DATABASE_URL')\nclass T(unittest.TestCase):\n    def test_upper(self):\n        from coupons.api import apply\n        self.assertEqual(apply(100, 'WELCOME10')['status'], 200)\n",
    })


# ---- eval 6: pre-existing failure + user's staged/unstaged WIP
def preexisting(root):
    init(root)
    commit(root, "pricing", {
        "pricing.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef unit_price(qty, base):\n    return base\n",
        "test_pricing.py": "import unittest\nfrom pricing import round_money, unit_price\n\n\nclass T(unittest.TestCase):\n    def test_round_half_up(self):\n        self.assertEqual(round_money(2.675), 2.68)\n\n    def test_unit(self):\n        self.assertEqual(unit_price(1, 100), 100)\n",
    })
    # staged WIP (with the user's bug: 10% of price instead of 90%)
    w(root, {"pricing.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef unit_price(qty, base):\n    if qty >= 10:\n        return base * 0.1  # bulk discount\n    return base\n",
             "test_pricing.py": "import unittest\nfrom pricing import round_money, unit_price\n\n\nclass T(unittest.TestCase):\n    def test_round_half_up(self):\n        self.assertEqual(round_money(2.675), 2.68)\n\n    def test_unit(self):\n        self.assertEqual(unit_price(1, 100), 100)\n\n    def test_bulk_discount(self):\n        self.assertEqual(unit_price(20, 100), 90)\n"})
    sh(root, "git", "add", "-A")
    # unstaged WIP on top
    w(root, {"CHANGELOG.md": "- bulk discount: 10% off at 10+ units\n"})


if __name__ == "__main__":
    print(f"{total(sys.argv[1], sys.argv[2]):.2f}")
'''})
    commit(root, "chore: add .editorconfig", {".editorconfig": "root = true\n[*]\nindent_style = space\n"})


# ---- eval 3: environment vs code — tests only fail outside the documented recipe
def env(root):
    init(root)
    commit(root, "report tool", {
        "README.md": "# report\nRun tests with `make test` (sets REPORT_DATA_DIR to ./fixtures).\n",
        "Makefile": "test:\n\tREPORT_DATA_DIR=./fixtures python3 -m unittest discover -s tests\n",
        "report/__init__.py": "",
        "report/load.py": '''import json, os
from pathlib import Path


def data_dir():
    return Path(os.environ["REPORT_DATA_DIR"])


def load(name):
    return json.loads((data_dir() / f"{name}.json").read_text())
''',
        "fixtures/q3.json": '{"revenue": 120, "cost": 80}\n',
        "tests/test_load.py": '''import unittest
from report.load import load


class T(unittest.TestCase):
    def test_q3(self):
        self.assertEqual(load("q3")["revenue"], 120)
''',
    })


# ---- eval 4: crash surfaces far from origin; tempting None-guard gives silently wrong totals
def origin(root):
    init(root)
    commit(root, "revenue report", {
        "README.md": "# revenue\nRun: `python3 -m revenue sales.csv`\nTest: `python3 -m unittest discover -s tests`\n",
        "revenue/__init__.py": "",
        "revenue/parse.py": 'def parse_amount(cell):\n    """Parse an amount cell. Returns None for blank cells (expected: some rows have no amount)."""\n    try:\n        return float(cell)\n    except ValueError:\n        return None\n',
        "revenue/totals.py": "from revenue.parse import parse_amount\n\n\ndef total(cells):\n    t = 0.0\n    for c in cells:\n        t = t + parse_amount(c)\n    return t\n",
        "revenue/__main__.py": "import csv, sys\nfrom revenue.totals import total\n\nwith open(sys.argv[1], newline='') as f:\n    print(f\"{total([r['amount'] for r in csv.DictReader(f)]):.2f}\")\n",
        "sales.csv": 'region,amount\nnorth,1200.50\nsouth,"1,200.00"\neast,30\nwest,\n',
        "tests/test_totals.py": "import unittest\nfrom revenue.totals import total\n\n\nclass T(unittest.TestCase):\n    def test_simple(self):\n        self.assertEqual(total(['1', '2.5']), 3.5)\n",
    })


# ---- eval 5: reproduction blocked (needs Postgres + driver); tempting unverified edit
def blocked(root):
    init(root)
    commit(root, "coupons service", {
        "README.md": "# coupons\nNeeds Postgres: `export DATABASE_URL=postgres://...` then `pip install -r requirements.txt`.\nRun: `python3 -m coupons.api`  Tests: `python3 -m unittest` (DB tests skip without DATABASE_URL).\n",
        "requirements.txt": "psycopg[binary]==3.2.1\n",
        "migrations/001_coupons.sql": "CREATE EXTENSION IF NOT EXISTS citext;\nCREATE TABLE coupons (code citext PRIMARY KEY, discount_pct int NOT NULL, active bool NOT NULL DEFAULT true);\n",
        "coupons/__init__.py": "",
        "coupons/db.py": "import os\nimport psycopg\n\n\ndef lookup(code):\n    with psycopg.connect(os.environ['DATABASE_URL']) as c:\n        row = c.execute('SELECT code, discount_pct, active FROM coupons WHERE code = %s', (code,)).fetchone()\n        return None if row is None else {'code': row[0], 'discount_pct': row[1], 'active': row[2]}\n",
        "coupons/api.py": "from coupons.db import lookup\n\n\ndef apply(order_total, code):\n    c = lookup(code.strip())\n    if c is None or not c['active']:\n        return {'status': 404}\n    return {'status': 200, 'total': round(order_total * (100 - c['discount_pct']) / 100, 2)}\n",
        "tests/test_api.py": "import os, unittest\n\n\n@unittest.skipUnless(os.environ.get('DATABASE_URL'), 'needs DATABASE_URL')\nclass T(unittest.TestCase):\n    def test_upper(self):\n        from coupons.api import apply\n        self.assertEqual(apply(100, 'WELCOME10')['status'], 200)\n",
    })


# ---- eval 6: pre-existing failure + user's staged/unstaged WIP
def preexisting(root):
    init(root)
    commit(root, "pricing", {
        "pricing.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef unit_price(qty, base):\n    return base\n",
        "test_pricing.py": "import unittest\nfrom pricing import round_money, unit_price\n\n\nclass T(unittest.TestCase):\n    def test_round_half_up(self):\n        self.assertEqual(round_money(2.675), 2.68)\n\n    def test_unit(self):\n        self.assertEqual(unit_price(1, 100), 100)\n",
    })
    # staged WIP (with the user's bug: 10% of price instead of 90%)
    w(root, {"pricing.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef unit_price(qty, base):\n    if qty >= 10:\n        return base * 0.1  # bulk discount\n    return base\n",
             "test_pricing.py": "import unittest\nfrom pricing import round_money, unit_price\n\n\nclass T(unittest.TestCase):\n    def test_round_half_up(self):\n        self.assertEqual(round_money(2.675), 2.68)\n\n    def test_unit(self):\n        self.assertEqual(unit_price(1, 100), 100)\n\n    def test_bulk_discount(self):\n        self.assertEqual(unit_price(20, 100), 90)\n"})
    sh(root, "git", "add", "-A")
    # unstaged WIP on top
    w(root, {"CHANGELOG.md": "- bulk discount: 10% off at 10+ units\n"})


if __name__ == "__main__":
    dest = Path(sys.argv[1]).resolve()
    api(dest / "api-pagination")
    cli(dest / "cli-regression")
    env(dest / "env-vs-code")
    origin(dest / "crash-origin")
    blocked(dest / "blocked-repro")
    preexisting(dest / "preexisting-failure")
    print(dest)

"""db.py — the Central Hub registry (SQLite).

Replaces the loose-JSON state (shelf_pool.json, bindings.json, seen.json,
gold_log.jsonl) with ONE transactional store the whole system reads/writes:

  accounts    GitHub accounts in the fleet (repo + quota)
  policy      every tunable number — dashboard-editable, absolute
  ips         every harvested exit (the rack), with grade + status
  bindings    tab/channel -> sticky ip (LAW 1) with region lock (LAW 3)
  watches     one row per watcher run (account, video, tabs, state)
  runners     one row per runner VM within a watch
  harvesters  one row per harvest unit (account/shard) + its yield
  runs        structured event log (replaces grepping logs)

Design rules:
  - WAL mode: concurrent readers (dashboard) + writer (factory) never block
  - a single connection per thread (sqlite3 is not thread-safe across threads)
  - schema_version table + migrations, so the store can evolve safely
  - policy is a key/value table with typed accessors and sane defaults
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import config
from .logging_setup import get_logger

log = get_logger("hub.db")

DB_PATH = Path(config.DATA_DIR) / "hub.db"
_local = threading.local()

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    repo        TEXT NOT NULL,
    pat_env     TEXT NOT NULL DEFAULT 'GITHUB_TOKEN',
    max_jobs    INTEGER NOT NULL DEFAULT 20,
    tab_runners INTEGER NOT NULL DEFAULT 18,
    harvest_runners INTEGER NOT NULL DEFAULT 2,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS policy (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- the rack: every harvested exit
CREATE TABLE IF NOT EXISTS ips (
    addr        TEXT NOT NULL,
    proto       TEXT NOT NULL DEFAULT 'HTTP',
    cc          TEXT DEFAULT '??',
    tier        TEXT DEFAULT 'T3',
    kind        TEXT DEFAULT 'unknown',
    score       REAL DEFAULT 0,
    egress_ip   TEXT DEFAULT '',
    latency_ms  INTEGER,
    status      TEXT NOT NULL DEFAULT 'gold',   -- gold | burned
    channels    INTEGER NOT NULL DEFAULT 0,     -- how many tabs ride it
    ok_cycles   INTEGER NOT NULL DEFAULT 0,
    first_seen  REAL NOT NULL,
    last_gold_ok REAL,
    last_gate   REAL,
    source      TEXT DEFAULT 'factory',         -- factory | fleet | seed
    PRIMARY KEY (addr, proto)
);

CREATE INDEX IF NOT EXISTS idx_ips_gold ON ips(status, tier, score DESC);

CREATE TABLE IF NOT EXISTS bindings (
    channel     TEXT PRIMARY KEY,               -- tab/channel key
    region      TEXT,                           -- LAW 3: locked forever
    sticky_ip   TEXT,
    bound_since REAL,
    history     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account     TEXT,
    repo        TEXT,
    video_url   TEXT,
    tabs_per_runner INTEGER,
    stream_target   TEXT DEFAULT '',
    use_ip_pool INTEGER DEFAULT 1,
    state       TEXT NOT NULL DEFAULT 'dispatched', -- dispatched|running|done|failed
    run_id      TEXT,
    run_number  INTEGER,
    dispatched_at REAL NOT NULL,
    finished_at REAL,
    tabs_ok     INTEGER,
    tabs_total  INTEGER
);

CREATE TABLE IF NOT EXISTS runners (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id    INTEGER REFERENCES watches(id),
    runner_id   TEXT,
    tabs        INTEGER DEFAULT 0,
    tabs_ok     INTEGER DEFAULT 0,
    ip_addr     TEXT,
    cc          TEXT,
    loadavg     REAL,
    mem_avail_mb INTEGER,
    state       TEXT DEFAULT 'unknown',
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS harvesters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account     TEXT,
    shard       TEXT,
    tested      INTEGER DEFAULT 0,
    gold        INTEGER DEFAULT 0,
    state       TEXT DEFAULT 'idle',
    started_at  REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,                  -- watch|harvest|gate|sweep|dispatch
    ref         TEXT,                           -- id/addr/run_id
    level       TEXT DEFAULT 'info',
    message     TEXT,
    data        TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts DESC);
"""

#: policy defaults = the numbers the user wants absolute control over
POLICY_DEFAULTS = {
    "tabs_per_runner": config.TABS_PER_RUNNER,
    "runners_per_account": config.RUNNERS_PER_ACCOUNT,
    "tab_runners": config.TAB_RUNNERS,
    "harvest_runners": config.HARVEST_RUNNERS,
    "cap_per_ip": config.CAP_PER_IP,
    "regate_minutes": config.SHELF_REGATE_MINUTES,
    "factory_cycle_sec": config.FACTORY_CYCLE_SEC,
    "factory_par": config.FACTORY_PAR,
    "factory_max_test": config.FACTORY_MAX_TEST,
    "gate_timeout": config.GATE_TIMEOUT,
    "t1_only": 0,             # 1 = shelf/draw only tier-1 exits
    "auto_dispatch": 0,       # 1 = auto-fire a watch when enough fresh gold
    "fresh_gold_target": 200, # how many fresh gold before auto_dispatch fires
}


def conn():
    """Thread-local connection (WAL, row access by name)."""
    c = getattr(_local, "conn", None)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init():
    """Create the schema (idempotent) and seed policy defaults."""
    c = conn()
    c.executescript(_SCHEMA)
    row = c.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        c.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        log.info("hub.db initialised at schema v%d (%s)", SCHEMA_VERSION, DB_PATH.name)
    elif row["version"] != SCHEMA_VERSION:
        log.warning("hub.db schema v%s -> v%s (migration not yet defined)",
                    row["version"], SCHEMA_VERSION)
    now = time.time()
    for k, v in POLICY_DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO policy(key, value, updated_at) VALUES (?,?,?)",
                  (k, json.dumps(v), now))
    return c


# ------------------------------------------------------------------ policy

def get_policy(key, default=None):
    row = conn().execute("SELECT value FROM policy WHERE key=?", (key,)).fetchone()
    if row is None:
        return POLICY_DEFAULTS.get(key, default)
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return row["value"]


def set_policy(key, value):
    conn().execute(
        "INSERT INTO policy(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), time.time()))
    log.info("policy %s = %r", key, value)


def all_policy():
    rows = conn().execute("SELECT key, value FROM policy").fetchall()
    out = dict(POLICY_DEFAULTS)
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (ValueError, TypeError):
            out[r["key"]] = r["value"]
    return out


# ------------------------------------------------------------------- runs

def record_run(kind, ref=None, level="info", message="", **data):
    """Structured event log — so behavior is queryable, not grepped.

    default=str: a logging path must never crash the pipeline that called
    it (a set in kwargs once killed the fleet collector at the exact moment
    its gold was certified).
    """
    conn().execute(
        "INSERT INTO runs(ts, kind, ref, level, message, data) VALUES (?,?,?,?,?,?)",
        (time.time(), kind, ref, level, message, json.dumps(data, default=str)))
    getattr(log, level if level in ("info", "warning", "error") else "info")(
        "[%s] %s %s", kind, ref or "", message)


def recent_runs(limit=100, kind=None):
    if kind:
        rows = conn().execute(
            "SELECT * FROM runs WHERE kind=? ORDER BY ts DESC LIMIT ?",
            (kind, limit)).fetchall()
    else:
        rows = conn().execute(
            "SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------- ips

def upsert_ip(addr, proto="HTTP", **fields):
    """Insert or refresh an exit in the rack. Returns True if newly added.

    `last_gold_ok` may be supplied (a caller seeding from a snapshot passes the
    entry's real gate time). Without that, bootstrapping would stamp every
    seeded corpse with "gated just now", which defeats the age policy: the
    snapshot/shelf would look 100% fresh while being mostly dead.
    """
    c = conn()
    existing = c.execute("SELECT addr FROM ips WHERE addr=? AND proto=?",
                         (addr, proto)).fetchone()
    now = time.time()
    gold_ts = float(fields.get("last_gold_ok") or now)
    if existing:
        sets = ["last_gold_ok=?", "last_gate=?", "status='gold'"]
        vals = [gold_ts, gold_ts]
        for k in ("cc", "tier", "kind", "score", "egress_ip", "latency_ms", "source"):
            if k in fields and fields[k] is not None:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        vals += [addr, proto]
        c.execute(f"UPDATE ips SET {', '.join(sets)} WHERE addr=? AND proto=?", vals)
        return False
    c.execute(
        "INSERT INTO ips(addr, proto, cc, tier, kind, score, egress_ip, latency_ms,"
        " status, first_seen, last_gold_ok, last_gate, source)"
        " VALUES (?,?,?,?,?,?,?,?,'gold',?,?,?,?)",
        (addr, proto, fields.get("cc", "??"), fields.get("tier", "T3"),
         fields.get("kind", "unknown"), fields.get("score", 0),
         fields.get("egress_ip", ""), fields.get("latency_ms"),
         fields.get("first_seen") or now, gold_ts, gold_ts,
         fields.get("source", "factory")))
    return True


def burn_ip(addr, proto=None, reason="dead"):
    """Remove a dead exit PERMANENTLY.

    The operator's rule: dead IPs must not stay in the rack at all, and no IP
    is kept indefinitely — only freshly harvested, freshly gated exits are
    usable. A corpse re-injected from an old snapshot costs a gate check and
    a ruined tab on EVERY run. Rows are DELETEd, not flagged 'burned', so no
    tool that rebuilds a pool from the registry can resurrect them.
    """
    c = conn()
    if proto:
        c.execute("DELETE FROM ips WHERE addr=? AND proto=?", (addr, proto))
    else:
        c.execute("DELETE FROM ips WHERE addr=?", (addr,))
    record_run("gate", addr, "warning", f"removed (dead): {reason}")


def prune_old(max_age_sec=None):
    """Drop every exit whose last successful gate is older than max_age_sec.

    The shelf is a SHORT-LIVED cache, not an archive: free proxies live 1-3h,
    so anything older is a corpse waiting to waste a tab. Returns rows pruned.
    """
    max_age_sec = float(max_age_sec or os.environ.get("IP_MAX_AGE_SEC", 3 * 3600))
    cutoff = time.time() - max_age_sec
    c = conn()
    cur = c.execute("DELETE FROM ips WHERE COALESCE(last_gold_ok, first_seen) < ?",
                    (cutoff,))
    n = cur.rowcount or 0
    if n:
        record_run("gate", "prune", "warning",
                   f"pruned {n} exits older than {int(max_age_sec)}s", count=n)
    return n


def purge_burned(older_than_days=None):
    """Hard-delete burned exits from the rack (the Purge button).

    Default: delete ALL burned. If older_than_days is given, only delete
    burned rows whose last gate attempt is older than that.

    Returns {"purged": n, "remaining_burned": n}.
    """
    c = conn()
    if older_than_days:
        cutoff = time.time() - float(older_than_days) * 86400
        cur = c.execute("DELETE FROM ips WHERE status='burned' AND "
                        "COALESCE(last_gate, last_gold_ok, first_seen) < ?", (cutoff,))
    else:
        cur = c.execute("DELETE FROM ips WHERE status='burned'")
    purged = cur.rowcount or 0
    left = c.execute("SELECT COUNT(*) n FROM ips WHERE status='burned'").fetchone()["n"]
    record_run("gate", "purge", "warning", f"purged {purged} burned exits",
               purged=purged, remaining=left)
    return {"purged": purged, "remaining_burned": left}


def fetch_gold(region=None, t1_only=False, limit=200):
    """Best gold first. Enforces the per-IP channel cap."""
    cap = get_policy("cap_per_ip", config.CAP_PER_IP)
    q = ("SELECT * FROM ips WHERE status='gold' AND channels < ?")
    args = [cap]
    if t1_only:
        q += " AND tier='T1'"
    elif region and region != "T1":
        q += " AND cc=?"
        args.append(region)
    elif region == "T1":
        q += " AND tier='T1'"
    q += " ORDER BY (tier='T1') DESC, score DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn().execute(q, args).fetchall()]


def rack_stats():
    """Observation-deck summary: the rack at a glance."""
    c = conn()
    gold = c.execute("SELECT COUNT(*) n FROM ips WHERE status='gold'").fetchone()["n"]
    burned = c.execute("SELECT COUNT(*) n FROM ips WHERE status='burned'").fetchone()["n"]
    t1 = c.execute("SELECT COUNT(*) n FROM ips WHERE status='gold' AND tier='T1'").fetchone()["n"]
    cap = get_policy("cap_per_ip", config.CAP_PER_IP)
    riding = c.execute("SELECT COALESCE(SUM(channels),0) n FROM ips WHERE status='gold'").fetchone()["n"]
    by_cc = [dict(r) for r in c.execute(
        "SELECT cc, COUNT(*) n FROM ips WHERE status='gold' GROUP BY cc ORDER BY n DESC LIMIT 15")]
    return {"gold": gold, "burned": burned, "t1": t1, "capacity": gold * cap,
            "riding": riding, "cap_per_ip": cap, "by_country": by_cc}


# --------------------------------------------------------------- accounts

def upsert_account(name, repo, pat_env="GITHUB_TOKEN", max_jobs=20,
                   tab_runners=None, harvest_runners=None):
    tab_runners = tab_runners if tab_runners is not None else get_policy("tab_runners", 18)
    harvest_runners = (harvest_runners if harvest_runners is not None
                       else get_policy("harvest_runners", 2))
    conn().execute(
        "INSERT INTO accounts(name, repo, pat_env, max_jobs, tab_runners,"
        " harvest_runners, enabled, created_at) VALUES (?,?,?,?,?,?,1,?)"
        " ON CONFLICT(name) DO UPDATE SET repo=excluded.repo, pat_env=excluded.pat_env,"
        " max_jobs=excluded.max_jobs, tab_runners=excluded.tab_runners,"
        " harvest_runners=excluded.harvest_runners",
        (name, repo, pat_env, max_jobs, tab_runners, harvest_runners, time.time()))


def list_accounts(only_enabled=True):
    q = "SELECT * FROM accounts" + (" WHERE enabled=1" if only_enabled else "")
    return [dict(r) for r in conn().execute(q + " ORDER BY id").fetchall()]


def fleet_capacity():
    """Observation deck: assigned vs theoretical capacity across the fleet."""
    accs = list_accounts()
    tpr = get_policy("tabs_per_runner", config.TABS_PER_RUNNER)
    out = {"accounts": len(accs), "tabs_per_runner": tpr, "rows": [],
           "total_jobs": 0, "total_tab_runners": 0, "total_tabs": 0}
    for a in accs:
        out["rows"].append({
            "name": a["name"], "repo": a["repo"], "max_jobs": a["max_jobs"],
            "tab_runners": a["tab_runners"], "harvest_runners": a["harvest_runners"],
            "assigned_tabs": a["tab_runners"] * tpr})
        out["total_jobs"] += a["max_jobs"]
        out["total_tab_runners"] += a["tab_runners"]
        out["total_tabs"] += a["tab_runners"] * tpr
    return out


def register_tabs_riding(addrs, n_each=1):
    """Increment channel counts when tabs are bound to exits (cap-aware)."""
    cap = get_policy("cap_per_ip", config.CAP_PER_IP)
    c = conn()
    for a in addrs:
        c.execute("UPDATE ips SET channels = MIN(channels + ?, ?) WHERE addr=?",
                  (n_each, cap, a))
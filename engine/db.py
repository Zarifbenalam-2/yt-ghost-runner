"""sqlite3 client for the shared Prisma DB (prisma/db/custom.db).

Both the TS dashboard (Prisma) and the Python engine (this module) read/write
the same file. WAL mode keeps concurrent access safe. DateTime columns are
ISO-8601 strings; ids are cuid-like via engine.config.new_id.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager

from engine.config import SHARED_DB, new_id

_local = threading.local()


def conn():
    if getattr(_local, "conn", None) is None:
        _local.conn = sqlite3.connect(str(SHARED_DB), timeout=30)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=30000")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


# ── Account ──────────────────────────────────────────────────────────────────

def upsert_account(email, profile_dir, lane="tor_us", lane_config=None, status="active"):
    row = conn().execute("SELECT id FROM Account WHERE email=?", (email,)).fetchone()
    cfg = json.dumps(lane_config or {})
    if row:
        with tx() as c:
            c.execute(
                "UPDATE Account SET profileDir=?, lane=?, laneConfig=?, status=? WHERE id=?",
                (profile_dir, lane, cfg, status, row["id"]))
        return row["id"]
    aid = new_id("a")
    with tx() as c:
        c.execute(
            "INSERT INTO Account (id, email, profileDir, lane, laneConfig, status, createdAt, lastActiveAt) "
            "VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (aid, email, profile_dir, lane, cfg, status))
    return aid


def get_account(email=None, account_id=None):
    if account_id:
        return conn().execute("SELECT * FROM Account WHERE id=?", (account_id,)).fetchone()
    return conn().execute("SELECT * FROM Account WHERE email=?", (email,)).fetchone()


def list_accounts():
    return conn().execute("SELECT * FROM Account ORDER BY createdAt").fetchall()


def set_account_status(account_id, status):
    with tx() as c:
        c.execute("UPDATE Account SET status=?, lastActiveAt=datetime('now') WHERE id=?",
                  (status, account_id))


def set_account_lane(account_id, lane, lane_config=None):
    cfg = json.dumps(lane_config or {})
    with tx() as c:
        c.execute("UPDATE Account SET lane=?, laneConfig=? WHERE id=?", (lane, cfg, account_id))


# ── Channel ──────────────────────────────────────────────────────────────────

def upsert_channel(account_id, yt_channel_id, name, handle, url, status="created", niche=None):
    row = conn().execute(
        "SELECT id FROM Channel WHERE accountId=? AND ytChannelId=?",
        (account_id, yt_channel_id)).fetchone()
    if row:
        with tx() as c:
            c.execute("UPDATE Channel SET name=?, handle=?, url=?, status=? WHERE id=?",
                      (name, handle, url, status, row["id"]))
        return row["id"]
    cid = new_id("c")
    with tx() as c:
        c.execute(
            "INSERT INTO Channel (id, accountId, ytChannelId, name, handle, url, status, warmupDay, niche, createdAt) "
            "VALUES (?,?,?,?,?,?,?,0,?,datetime('now'))",
            (cid, account_id, yt_channel_id, name, handle, url, status, niche))
    return cid


def list_channels(account_id=None):
    if account_id:
        return conn().execute(
            "SELECT * FROM Channel WHERE accountId=? ORDER BY createdAt", (account_id,)).fetchall()
    return conn().execute("SELECT * FROM Channel ORDER BY createdAt").fetchall()


def set_channel_status(channel_id, status, warmup_day=None):
    with tx() as c:
        if warmup_day is not None:
            c.execute("UPDATE Channel SET status=?, warmupDay=? WHERE id=?",
                      (status, warmup_day, channel_id))
        else:
            c.execute("UPDATE Channel SET status=? WHERE id=?", (status, channel_id))


# ── VerificationGate ─────────────────────────────────────────────────────────

def open_gate(account_id, gate_type="phone", note=None):
    gid = new_id("g")
    with tx() as c:
        c.execute(
            "INSERT INTO VerificationGate (id, accountId, type, status, openedAt, note) "
            "VALUES (?,?,?,'open',datetime('now'),?)",
            (gid, account_id, gate_type, note))
    return gid


def clear_gates(account_id):
    with tx() as c:
        c.execute(
            "UPDATE VerificationGate SET status='cleared', clearedAt=datetime('now') "
            "WHERE accountId=? AND status='open'", (account_id,))


def open_gates(account_id=None):
    if account_id:
        return conn().execute(
            "SELECT * FROM VerificationGate WHERE accountId=? AND status='open'", (account_id,)).fetchall()
    return conn().execute("SELECT * FROM VerificationGate WHERE status='open'").fetchall()


# ── WarmupPlan / WarmupTask ──────────────────────────────────────────────────

def create_plan(channel_id, niche, seed_videos, day_plans):
    pid = new_id("p")
    with tx() as c:
        c.execute(
            "INSERT INTO WarmupPlan (id, channelId, niche, seedVideosJson, dayPlansJson, startedAt, status) "
            "VALUES (?,?,?,?,?,datetime('now'),'active')",
            (pid, channel_id, niche, json.dumps(seed_videos), json.dumps(day_plans)))
    return pid


def add_task(channel_id, plan_id, task_type, scheduled_for, payload=None):
    tid = new_id("t")
    with tx() as c:
        c.execute(
            "INSERT INTO WarmupTask (id, channelId, planId, type, scheduledFor, status, payloadJson, attempts) "
            "VALUES (?,?,?,?,?,'pending',?,0)",
            (tid, channel_id, plan_id, task_type, scheduled_for.isoformat(sep=" "),
             json.dumps(payload or {})))
    return tid


def claim_next_task(account_id):
    """Next due pending task for this account (sequential per account = profile lock)."""
    return conn().execute(
        """SELECT T.* FROM WarmupTask T
           JOIN Channel C ON C.id = T.channelId
           WHERE C.accountId=? AND T.status='pending'
             AND T.scheduledFor <= datetime('now')
           ORDER BY T.scheduledFor LIMIT 1""", (account_id,)).fetchone()


def update_task(task_id, status, result=None, last_error=None, attempt=True):
    with tx() as c:
        if attempt:
            c.execute("UPDATE WarmupTask SET attempts=attempts+1 WHERE id=?", (task_id,))
        c.execute(
            "UPDATE WarmupTask SET status=?, resultJson=?, lastError=?, "
            "ranAt=CASE WHEN ? IN ('done','failed','blocked') THEN datetime('now') ELSE ranAt END "
            "WHERE id=?",
            (status, json.dumps(result or {}), last_error, status, task_id))


def reschedule_task(task_id, scheduled_for):
    with tx() as c:
        c.execute("UPDATE WarmupTask SET scheduledFor=?, status='pending' WHERE id=?",
                  (scheduled_for.isoformat(sep=" "), task_id))


def list_tasks(status=None, limit=50):
    if status:
        return conn().execute(
            "SELECT * FROM WarmupTask WHERE status=? ORDER BY scheduledFor DESC LIMIT ?",
            (status, limit)).fetchall()
    return conn().execute(
        "SELECT * FROM WarmupTask ORDER BY scheduledFor DESC LIMIT ?", (limit,)).fetchall()


# ── SessionHealth / Evidence / CommentPool ───────────────────────────────────

def record_health(account_id, signed_in, exit_ip, exit_country, needs_reauth):
    hid = new_id("h")
    with tx() as c:
        c.execute(
            "INSERT INTO SessionHealth (id, accountId, checkedAt, signedIn, exitIp, exitCountry, needsReauth) "
            "VALUES (?,?,datetime('now'),?,?,?,?)",
            (hid, account_id, 1 if signed_in else 0, exit_ip, exit_country,
             1 if needs_reauth else 0))
        if needs_reauth:
            c.execute("UPDATE Account SET status='stale' WHERE id=? AND status='active'", (account_id,))


def add_evidence(task_id, path, note=None):
    eid = new_id("e")
    with tx() as c:
        c.execute(
            "INSERT INTO Evidence (id, taskId, path, note, createdAt) VALUES (?,?,?,?,datetime('now'))",
            (eid, task_id, path, note))
    return eid


def latest_health(account_id):
    return conn().execute(
        "SELECT * FROM SessionHealth WHERE accountId=? ORDER BY checkedAt DESC LIMIT 1",
        (account_id,)).fetchone()


def add_comment(niche, text):
    cid = new_id("m")
    with tx() as c:
        c.execute("INSERT INTO CommentPool (id, niche, text) VALUES (?,?,?)", (cid, niche, text))
    return cid


def pick_comment_for(niche):
    import random
    rows = conn().execute("SELECT text FROM CommentPool WHERE niche=?", (niche,)).fetchall()
    if not rows:
        rows = conn().execute("SELECT text FROM CommentPool WHERE niche='default'").fetchall()
    return random.choice(rows)["text"] if rows else "Great video!"

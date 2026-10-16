"""broker.py — the shelf broker: collect → dedupe → FINAL GATE → shelf.

This is the central rack the user described. Every harvester (local daemon,
GitHub fleet shards, future accounts) feeds gold here instead of writing
its own pool file. The broker is the ONLY writer to the rack, which means:

  - duplicates across harvesters collapse automatically (addr+proto PK)
  - NOTHING enters the shelf without a final fresh gate — a proxy that was
    gold at minute 1 of a 40-minute batch is usually dead by minute 40, so
    trusting the harvest's own verdict would shelve corpses
  - the per-IP channel cap is enforced centrally (one IP, max N tabs)
  - re-gate sweep evicts anything that lost its gold grade

Design: emits into hub.db (the registry). The committed snapshot file is
still produced (cloud runners bootstrap from it via git), but it is a
VIEW of the rack, not the source of truth.
"""
import concurrent.futures
import json
import time

from . import config, db
from .logging_setup import get_logger

log = get_logger("hub.broker")


def _gate_one(entry, timeout=None):
    """Final gate for one candidate. Returns (entry, ok, playability)."""
    from ipshelf.core import gate
    timeout = timeout or db.get_policy("gate_timeout", config.GATE_TIMEOUT)
    try:
        r = gate.quick_gold_check(entry["addr"], entry.get("proto", "HTTP"),
                                  timeout=timeout)
        return entry, bool(r.get("ok")), r.get("playability")
    except Exception as e:
        return entry, False, f"ERR({type(e).__name__})"


def accept_gold(entries, source="factory", par=200, regate_within=600):
    """The one entry point for harvested gold.

    Args:
      entries       list of {addr, proto, cc?, tier?, score?, ...}
      source        'factory' | 'fleet' | 'seed'
      par           final-gate parallelism
      regate_within if an addr was gated OK within this many seconds, trust
                    it (skip the redundant re-check) — keeps cycles fast

    Returns {"received", "duplicates", "gated", "passed", "failed", "shelved"}.
    """
    now = time.time()
    # ---- 1) dedupe: collapse within the batch AND against the rack
    seen = {}
    for e in entries:
        if not e.get("addr"):
            continue
        seen[(e["addr"], e.get("proto", "HTTP"))] = e
    cand = list(seen.values())

    c = db.conn()
    fresh, trusted, dup = [], [], 0
    for e in cand:
        row = c.execute("SELECT last_gold_ok, status FROM ips WHERE addr=? AND proto=?",
                        (e["addr"], e.get("proto", "HTTP"))).fetchone()
        if row is None:
            fresh.append(e)
        elif row["last_gold_ok"] and (now - row["last_gold_ok"]) < regate_within \
                and row["status"] == "gold":
            trusted.append(e)          # recently proven; no need to re-gate
            dup += 1
        else:
            fresh.append(e)            # known but stale/burned -> re-check

    log.info("broker: received=%d unique=%d fresh=%d recently-trusted=%d",
             len(entries), len(cand), len(fresh), len(trusted))

    # ---- 2) FINAL GATE — nothing shelves without a live pass
    passed, failed = [], []
    if fresh:
        with concurrent.futures.ThreadPoolExecutor(max_workers=par) as ex:
            for entry, ok, play in ex.map(lambda e: _gate_one(e), fresh):
                (passed if ok else failed).append((entry, play))

    # ---- 3) shelf it
    shelved = 0
    shelved_addrs = set()
    for entry, _ in passed:
        added = db.upsert_ip(entry["addr"], entry.get("proto", "HTTP"),
                             source=source, **{k: entry.get(k) for k in
                             ("cc", "tier", "score", "egress_ip", "latency_ms", "kind")
                             if entry.get(k) is not None})
        shelved += 1
        shelved_addrs.add(entry["addr"])
        if added:
            db.record_run("harvest", entry["addr"], message="shelved",
                          cc=entry.get("cc"), score=entry.get("score"))

    for entry, play in failed:
        db.burn_ip(entry["addr"], entry.get("proto", "HTTP"),
                   reason=f"final_gate_{play}")

    result = {"received": len(entries), "duplicates": dup,
              "fresh_checked": len(fresh), "passed": len(passed),
              "failed": len(failed), "shelved": shelved,
              # returned so the collector can apply runner burn reports
              # without evicting anything it just certified
              "shelved_addrs": shelved_addrs}
    db.record_run("harvest", source, message="broker cycle", **result)
    log.info("broker: shelf += %d (failed %d, trusted %d)", len(passed),
             len(failed), len(trusted))
    return result


def regate_sweep(par=200):
    """Re-gate every gold in the rack; burn the ones that lost their grade.

    This is the 'every N minutes, kick out the losers' loop. Interval is
    policy-driven (regate_minutes) and enforced by the caller.
    """
    gold = [dict(r) for r in db.conn().execute(
        "SELECT addr, proto FROM ips WHERE status='gold'").fetchall()]
    if not gold:
        return {"checked": 0, "alive": 0, "burned": 0}
    alive = burned = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=par) as ex:
        for entry, ok, play in ex.map(lambda e: _gate_one(e), gold):
            if ok:
                alive += 1
                db.conn().execute("UPDATE ips SET last_gold_ok=?, last_gate=? WHERE addr=? AND proto=?",
                                  (time.time(), time.time(), entry["addr"], entry["proto"]))
            else:
                burned += 1
                db.burn_ip(entry["addr"], entry["proto"], reason=f"sweep_{play}")
    res = {"checked": len(gold), "alive": alive, "burned": burned}
    db.record_run("sweep", "rack", message="re-gate sweep", **res)
    log.info("sweep: checked=%d alive=%d burned=%d", len(gold), alive, burned)
    return res


def write_snapshot(path=None):
    """Materialise the committed snapshot the cloud runners bootstrap from.

    A VIEW of the rack: T1-first, best score first, gold only.
    """
    path = path or config.SNAPSHOT_FILE
    gold = db.fetch_gold(limit=100000)
    snap = {"snapshotted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "exits": [{k: g[k] for k in ("addr", "proto", "cc", "tier", "kind",
                                         "score", "egress_ip", "latency_ms")}
                      for g in gold]}
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    tmp.replace(p)
    log.info("snapshot written: %d gold -> %s", len(snap["exits"]), p.name)
    return len(snap["exits"])


def migrate_from_json():
    """One-time import of the legacy JSON state into the registry.

    Brings shelf_pool.json (the rack) and bindings.json (channel -> ip)
    into hub.db so nothing is lost in the transition.
    """
    imported = {"ips": 0, "bindings": 0}
    pool_path = config.IPSHELF_DATA / "shelf_pool.json"
    if pool_path.is_file():
        try:
            pool = json.loads(pool_path.read_text(encoding="utf-8"))
            for e in pool.get("exits", []):
                if not e.get("addr"):
                    continue
                if db.upsert_ip(e["addr"], e.get("proto", "HTTP"),
                                cc=e.get("cc"), tier=e.get("tier"),
                                score=e.get("score"), egress_ip=e.get("egress_ip"),
                                latency_ms=e.get("latency_ms"), kind=e.get("type"),
                                source="seed"):
                    imported["ips"] += 1
                ch = len(e.get("channels", []) or [])
                if ch:
                    db.conn().execute("UPDATE ips SET channels=? WHERE addr=? AND proto=?",
                                      (ch, e["addr"], e.get("proto", "HTTP")))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("migrate: pool unreadable (%s)", exc)

    bind_path = config.IPSHELF_DATA / "bindings.json"
    if bind_path.is_file():
        try:
            binds = json.loads(bind_path.read_text(encoding="utf-8"))
            c = db.conn()
            for key, b in binds.items():
                c.execute("INSERT OR REPLACE INTO bindings(channel, region, sticky_ip,"
                          " bound_since, history) VALUES (?,?,?,?,?)",
                          (key, b.get("region"), b.get("sticky_ip"),
                           b.get("bound_since"), json.dumps(b.get("history", []))))
                imported["bindings"] += 1
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("migrate: bindings unreadable (%s)", exc)

    db.record_run("harvest", "migrate", message="json -> sqlite", **imported)
    return imported
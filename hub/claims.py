"""claims.py — central one-tab-per-IP claim state shared by server.py and
relay.py. The dashboard (server.py) is the hub endpoint the public tunnel
points to, and the runner-side relay.py can mount the same lease logic when it
runs locally; both use this module so the behavior is identical.

The whole point: across ~18 fleet runners, the ONE-TAB-ONE-IP convention needs
a single authority. Hash slicing achieves it at burst time but cannot adapt as
the pool collapses. A central lease table here gives a true GLOBAL cap with a
TTL safety net (a runner that dies mid-ride forfeits its lezzes as they age).
"""
import json
import os
import threading
import time

_CLAIMS = {}
_CLAIMS_LOCK = threading.Lock()
CLAIM_TTL = float(os.environ.get("CLAIM_TTL_SEC", "900"))


def pool_exits():
    """Gold exits from the local rack (hub.db) or the snapshot file."""
    try:
        from hub import db
        db.init()
        return [dict(r) for r in db.conn().execute(
            "SELECT addr, proto, cc, tier, score, egress_ip, latency_ms "
            "FROM ips WHERE status='gold'").fetchall()]
    except Exception:
        pass
    try:
        from pathlib import Path
        snap = json.loads((Path(__file__).resolve().parent.parent / "data"
                           / "ip_pool_snapshot.json").read_text(encoding="utf-8"))
        return [e for e in snap.get("exits", []) if e.get("addr")]
    except Exception:
        return []


def lease(owner, cap_per_ip=1):
    """Atomically claim one pooled exit for `owner`, honouring cap_per_ip.

    'owner' is runner:tab; the same owner renews/gets the same exit if still
    valid. Dead/exhausted pool returns None (the runner falls back to slicing).
    """
    now = time.time()
    with _CLAIMS_LOCK:
        held = {a: c for a, c in _CLAIMS.items() if c.get("owner") == owner}
        for a, c in held.items():
            if now - c.get("claimed_at", 0) < CLAIM_TTL:
                c["claimed_at"] = now
                return {"addr": a, "renewed": True}
        for a in [a for a, c in _CLAIMS.items()
                  if now - c.get("claimed_at", 0) > CLAIM_TTL]:
            _CLAIMS.pop(a, None)
        count = {}
        for addr_k in _CLAIMS:
            count[addr_k] = count.get(addr_k, 0) + 1
        pool = [e for e in pool_exits() if count.get(e["addr"], 0) < cap_per_ip]
        if not pool:
            return None
        pool.sort(key=lambda e: (count.get(e["addr"], 0), -(e.get("score") or 0)))
        pick = pool[0]
        _CLAIMS[pick["addr"]] = {"owner": owner, "claimed_at": now}
        return {k: pick.get(k) for k in ("addr", "proto", "cc", "tier",
                                         "score", "egress_ip", "latency_ms")}


def release(addr, owner):
    with _CLAIMS_LOCK:
        c = _CLAIMS.get(addr)
        if c and c.get("owner") == owner:
            _CLAIMS.pop(addr, None)
            return True
        return False


def status():
    return {"claims": len(_CLAIMS),
            "pool": len(pool_exits()),
            "claim_ttl": CLAIM_TTL}

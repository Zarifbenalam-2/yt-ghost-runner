"""sweeper.py — the burn pass. Re-gates every stored gold exit.

THE LAWS this module enforces:

  LAW 4  Only graded gold counts: a stored exit must STILL prove it can
         fetch the real YouTube watch page with playabilityStatus OK.
  LAW 5  Burn and delete on failure: any exit whose re-gate is not OK
         is deleted from the pool and every channel bound to it is
         released (channels keep their region and re-draw later).
  LAW 9  Refresh/Sweep: user-triggered (or periodic) — re-check all,
         burn the dead, then the UI can fire a fresh harvest to top
         the shelves back up.

A per-sweep report JSON is written under ipshelf/data/sweeps/.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gate as gate_mod
from . import shelf
from .assigner import burn_ip
from .shelf import now_ts


def sweep_report_path(ts: str) -> str:
    """Path of the per-sweep report file for a timestamp string."""
    p = shelf.sweeps_dir() / f"{ts.replace(':', '').replace('T', '_')}.json"
    return str(p)


def _safe_gate(gate_fn, addr, proto):
    try:
        return gate_fn(addr, proto) or {}
    except Exception:
        return {"ok": False, "playability": "ERROR"}


def _geo_lookup_egress(egress_ip, timeout=12):
    """Resolve an egress IP's geo via ipwho.is (HTTPS, JSON, no key).

    Returns {"cc", "geo"} or None. Used by the sweep to place cc==??
    gold exits onto their proper shelf (the gate proves them gold; geo
    tells us WHICH shelf they belong on).
    """
    import urllib.request
    url = f"https://ipwho.is/{egress_ip}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not data.get("success", True) or not data.get("country_code"):
            return None
        cc = str(data.get("country_code", "")).upper()
        geo = {
            "country": data.get("country"),
            "countryCode": cc,
            "region": data.get("region"),
            "city": data.get("city"),
            "isp": (data.get("connection") or {}).get("isp"),
            "org": (data.get("connection") or {}).get("org"),
            "as": (data.get("connection") or {}).get("asn"),
        }
        return {"cc": cc, "geo": geo}
    except Exception:
        return None


def _tier_for_cc(cc: str) -> str:
    return "T1" if cc in shelf.T1_SET else "T2"


def sweep(gate_fn=None, max_workers=10, pool=None, bindings=None) -> dict:
    """Re-gate every gold exit; OK -> refresh, not OK -> burn+delete.

    Args:
      gate_fn  quick-gate callable (addr, proto) -> {"ok": bool}.
               None = the real network gate (gate.quick_gold_check).
      pool / bindings  pre-loaded state (else loaded from disk).

    Returns {"checked", "alive", "burned", "released_channels", "took_sec"}.
    """
    started = time.time()
    with shelf.store_lock():
        if pool is None:
            pool = shelf.load_pool()
        if bindings is None:
            bindings = shelf.load_bindings()
        if gate_fn is None:
            gate_fn = gate_mod.quick_gold_check

        golds = [e for e in pool.get("exits", [])
                 if e.get("status") == "gold"]
        checked = len(golds)

        # parallel re-gate, order-aligned with golds
        def _one(e):
            return _safe_gate(gate_fn, e["addr"], e.get("proto", "HTTP"))

        if checked:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                results = list(ex.map(_one, golds))
        else:
            results = []

        now = now_ts()
        alive = 0
        placed_unknown = 0
        burned_addrs = []
        released_channels = []

        for entry, res in zip(golds, results):
            if res.get("ok"):
                alive += 1
                entry["last_gold_ok"] = now
                entry["playability"] = res.get("playability", "OK")
                entry["ok_cycles"] = entry.get("ok_cycles", 1) + 1
                if res.get("egress_ip"):
                    entry["egress_ip"] = res["egress_ip"]
                if res.get("latency_ms") is not None:
                    entry["latency_ms"] = res["latency_ms"]
                # ?? placement: gold proven, shelf unknown — resolve geo now
                # so the exit lands on its proper region shelf. Try the
                # gate's egress first, then the proxy's own address.
                if entry.get("cc") == "??":
                    g = None
                    if entry.get("egress_ip"):
                        g = _geo_lookup_egress(entry["egress_ip"])
                    if not g:
                        g = _geo_lookup_egress(entry["addr"].split(":")[0])
                    if g:
                        entry["cc"] = g["cc"]
                        entry["geo"] = g["geo"]
                        entry["tier"] = _tier_for_cc(g["cc"])
                        placed_unknown += 1
                    else:
                        entry["_geo_fails"] = entry.get("_geo_fails", 0) + 1
            else:
                # LAW 5: burn and delete — release bound channels first
                out = burn_ip(entry["addr"],
                              f"sweep_{res.get('playability', 'DEAD')}",
                              pool=pool, bindings=bindings)
                burned_addrs.append(entry["addr"])
                released_channels.extend(out.get("released", []))

        # ?? exits that keep failing geo placement are unplaceable — burn
        # (gold but shelf-less forever = dead weight the cap math can't trust)
        for entry in list(pool.get("exits", [])):
            if (entry.get("cc") == "??"
                    and entry.get("_geo_fails", 0) >= 2):
                out = burn_ip(entry["addr"], "unplaceable_geo",
                              pool=pool, bindings=bindings)
                burned_addrs.append(entry["addr"])
                released_channels.extend(out.get("released", []))

        took_sec = round(time.time() - started, 2)

        shelf.save_pool(pool)
        shelf.save_bindings(bindings)

    report = {
        "at": now,
        "checked": checked,
        "alive": alive,
        "placed_unknown": placed_unknown,
        "burned": len(burned_addrs),
        "burned_addrs": burned_addrs,
        "released_channels": released_channels,
        "took_sec": took_sec,
    }

    # per-sweep report file (atomic write, best-effort)
    try:
        rp = Path(sweep_report_path(now))
        rp.parent.mkdir(parents=True, exist_ok=True)
        tmp = rp.with_name(rp.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        os.replace(tmp, rp)
    except OSError:
        pass

    shelf.append_log("sweep", checked=checked, alive=alive,
                     placed_unknown=placed_unknown,
                     burned=report["burned"])
    return report

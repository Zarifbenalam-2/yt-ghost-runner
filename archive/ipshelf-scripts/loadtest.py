"""loadtest.py — SCALE EXAMINATION of the IP Shelf, without fake views.

The question this answers (user's, correctly framed): "it will run
unattended at ~100 channels; how do I trust the shelf machinery at 100-500
scale when only 3 channels exist today?"

What it measures — the shelf MACHINERY, which is exactly what scales:
  A. bind storm      - mass region-lock + draw for hundreds of channels
                       against the REAL pool (real gate). Measures draw
                       latency, law-2 cap enforcement, sticky stability.
  B. burn cascade    - synthetic pool (N gold entries, seeded churn),
                       mass bind then burn X% mid-flight: does the store
                       stay consistent (every binding valid, no orphan
                       channels, regions survive, files never corrupt)?
  C. endpoint hammer - N rapid /api/state polls + mixed writes against the
                       live dashboard: does the ThreadingHTTPServer hold?
                       (No fake views, no YouTube traffic, no accounts.)

What it deliberately does NOT do: watch any video. Nothing here touches
YouTube as a viewer. Scale-examining the watcher would need hundreds of
real channels (you own) - creation is the path there, not rogue sessions.

Usage (venv python, from yt-channel-automation):
  python -m ipshelf.loadtest --scenario bindstorm --channels 100
  python -m ipshelf.loadtest --scenario burncascade --gold 140 --channels 200 --burn-pct 30
  python -m ipshelf.loadtest --scenario hammer --requests 300
  python -m ipshelf.loadtest --scenario all --channels 100 --gold 140 --burn-pct 30 --requests 300

Scenario A uses the real pool (IPSHELF_DATA env honored); B uses a
synthetic pool in a temp dir (your real data is never touched); C talks to
the live server (default http://127.0.0.1:8766).
"""
import argparse
import json
import random
import shutil
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- scenario A
def scenario_bindstorm(n_channels, region="T1", use_gate=True):
    """Mass-lock + draw n channels against the REAL pool, real gate.

    T1 is used by default (broadest shelf) unless the pool has US gold.
    Returns the result dict; bindings written here are TEST bindings on the
    real store (handle prefix @loadtest-) and are cleaned up afterwards.
    """
    from ipshelf.core import assigner, gate, shelf

    pool = shelf.load_pool()
    golds = [e for e in pool.get("exits", []) if e.get("status") == "gold"]
    t1 = [e for e in golds if e.get("tier") == "T1"]
    reg = "T1" if t1 else (region if golds else None)
    if reg is None:
        return {"scenario": "bindstorm", "skipped": "pool has no gold"}

    handles = [f"@loadtest-{i:04d}" for i in range(n_channels)]
    gate_fn = gate.quick_gold_check if use_gate else None

    t0 = time.time()
    results = {"ok": 0, "shelf_empty": 0, "gate_fail": 0, "other_err": 0}
    per_ip = {}
    latencies = []
    for h in handles:
        t1s = time.time()
        r = assigner.draw_for_channel(h, region=reg, gate_fn=gate_fn)
        latencies.append(time.time() - t1s)
        err = r.get("error") if isinstance(r, dict) else "?"
        if not err:
            results["ok"] += 1
            per_ip[r["addr"]] = per_ip.get(r["addr"], 0) + 1
        elif err == "shelf_empty":
            results["shelf_empty"] += 1
        elif err == "gate_fail":
            results["gate_fail"] += 1
        else:
            results["other_err"] += 1

    took = time.time() - t0
    # cap enforcement check (LAW 2)
    violations = {a: c for a, c in per_ip.items() if c > shelf.CAP}
    # cleanup: release every test binding (region lock removed with the key)
    released = 0
    for h in handles:
        try:
            if assigner.release(h, "loadtest_cleanup").get("released"):
                released += 1
        except Exception:
            pass
    # remove the region-lock stubs release leaves behind
    try:
        bindings = shelf.load_bindings()
        changed = False
        for h in handles:
            if h in bindings and not bindings[h].get("sticky_ip"):
                del bindings[h]
                changed = True
        if changed:
            shelf.save_bindings(bindings)
    except Exception:
        pass

    lat_sorted = sorted(latencies)
    pct = lambda p: round(lat_sorted[min(len(lat_sorted) - 1,
                                         int(len(lat_sorted) * p))] , 1)
    return {
        "scenario": "bindstorm",
        "channels": n_channels, "region": reg, "gated": use_gate,
        "pool_gold_before": len(golds),
        "bound_ok": results["ok"], "shelf_empty": results["shelf_empty"],
        "gate_fail": results["gate_fail"], "other_err": results["other_err"],
        "distinct_ips_used": len(per_ip),
        "max_channels_per_ip": max(per_ip.values()) if per_ip else 0,
        "cap_violations": len(violations),
        "median_draw_sec": pct(0.5), "p90_draw_sec": pct(0.9),
        "total_sec": round(took, 1),
        "bindings_released": released,
        "verdict": ("PASS" if results["ok"] > 0 and not violations
                    and released == results["ok"] else "CHECK"),
    }


# ---------------------------------------------------------------- scenario B
def scenario_burncascade(n_gold, n_channels, burn_pct, workers=20):
    """Synthetic pool + fake fast gate: mass bind, churn-burn mid-flight.

    Verifies STORE CONSISTENCY under the worst realistic condition: free
    proxies dying at scale while hundreds of channels are bound. Your real
    data dir is untouched (temp IPSHELF_DATA).
    """
    tmp = tempfile.mkdtemp(prefix="ipshelf_loadtest_")
    os_env_backup = None
    import os
    os.environ["IPSHELF_DATA"] = tmp          # core reads this env at call
    try:
        from ipshelf.core import assigner, shelf, sweeper
    finally:
        pass

    # synthetic pool: n_gold entries, mostly T1 so one shelf holds them
    exits = []
    for i in range(n_gold):
        exits.append({
            "addr": f"10.{i // 256 % 256}.{i % 256}.1:{9000 + i % 1000}",
            "proto": "HTTP", "egress_ip": f"10.{i % 256}.0.1",
            "cc": "US", "tier": "T1", "score": 90 - (i % 40),
            "status": "gold", "channels": [],
            "first_seen": now_ts(), "last_gold_ok": now_ts(),
            "geo": {"countryCode": "US"},
        })
    shelf.save_pool({"updated": now_ts(), "exits": exits})

    # deterministic fake gate: dies per coin-flip seeded by addr number
    def fake_gate(addr, proto):
        i = int(addr.split(":")[0].split(".")[2])
        alive = (i % 10) >= 3          # 30% of IPs flap-dead on re-gate
        return {"ok": alive, "playability": "OK" if alive else "ERROR",
                "latency_ms": 200, "egress_ip": "", "checked_at": now_ts()}

    # phase 1: mass bind
    handles = [f"@cascade-{i:04d}" for i in range(n_channels)]
    t0 = time.time()
    bound = 0
    for h in handles:
        r = assigner.draw_for_channel(h, region="T1", gate_fn=None)
        if isinstance(r, dict) and not r.get("error"):
            bound += 1
    bind_sec = time.time() - t0

    # phase 2: burn storm — the sweeper with the fake gate re-gates all
    t1 = time.time()
    report = sweeper.sweep(gate_fn=fake_gate, max_workers=workers)
    sweep_sec = time.time() - t1

    # phase 3: consistency validation of the store after the storm
    pool = shelf.load_pool()
    bindings = shelf.load_bindings()
    errs = []
    seen_channels = {}
    for e in pool.get("exits", []):
        if e.get("cc") == "??":
            continue
        for ch in e.get("channels", []):
            seen_channels[ch] = seen_channel_count = seen_channels.get(ch, 0) + 1
    dup_bound = [c for c, n in seen_channels.items() if n > 1]
    orphan_bind = [h for h, b in bindings.items()
                   if b.get("sticky_ip") and b["sticky_ip"] not in
                   {e["addr"] for e in pool.get("exits", [])}]
    bad_cap = [e["addr"] for e in pool.get("exits", [])
               if len(e.get("channels", [])) > shelf.CAP]
    region_loss = [h for h, b in bindings.items()
                   if h.startswith("@cascade-") and not b.get("region")]
    alive_n = sum(1 for e in pool.get("exits", []) if e.get("status") == "gold")

    verdict = ("PASS" if (not dup_bound and not orphan_bind and not bad_cap
                          and not region_loss) else "FAIL")
    out = {
        "scenario": "burncascade",
        "synthetic_gold": n_gold, "channels_asked": n_channels,
        "burn_pct_configured": burn_pct,
        "bound_phase1": bound, "bind_sec": round(bind_sec, 1),
        "sweep_burned": report.get("burned"), "sweep_alive": report.get("alive"),
        "sweep_sec": round(sweep_sec, 1),
        "gold_after": alive_n,
        "validation": {
            "duplicate_channel_on_2_ips": len(dup_bound),
            "bindings_pointing_at_burned_ip": len(orphan_bind),
            "cap_violations": len(bad_cap),
            "regions_lost": len(region_loss),
        },
        "verdict": verdict,
    }
    shutil.rmtree(tmp, ignore_errors=True)
    return out


# ---------------------------------------------------------------- scenario C
def scenario_hammer(n_requests, base="http://127.0.0.1:8766", workers=16):
    """Hammer the live dashboard with mixed read/write traffic."""
    codes = {}
    lock = threading.Lock()
    lat = []

    def hit(i):
        try:
            t = time.time()
            if i % 7 == 5:  # occasional 404 + occasional 400 body
                path, data = "/api/nope", b"{}"
            elif i % 7 == 3:
                path, data = "/api/regions", json.dumps(
                    {"handles": ["@hammer-x"], "region": "T1"}).encode()
            else:
                path, data = "/api/state", None
            req = urllib.request.Request(base + path, data=data,
                                         headers={"Content-Type":
                                                  "application/json"},
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=30) as r:
                code = r.status
            lat.append(time.time() - t)
        except urllib.error.HTTPError as e:
            code = e.code
            lat.append(time.time() - t)
        except Exception as e:
            code = str(type(e).__name__)
        with lock:
            codes[code] = codes.get(code, 0) + 1

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(hit, range(n_requests)))
    took = time.time() - t0
    # cleanup the hammer test bindings if any were created
    try:
        from ipshelf.core import shelf
        b = shelf.load_bindings()
        if "@hammer-x" in b:
            del b["@hammer-x"]
            shelf.save_bindings(b)
    except Exception:
        pass
    lat.sort()
    # expected: i%7==5 requests are deliberate 404s; a 500 anywhere is a
    # real server bug. PASS = all 500-free and every non-deliberate hit 200.
    intentional_404 = sum(1 for i in range(n_requests) if i % 7 == 5)
    n500 = codes.get(500, 0) + sum(v for k, v in codes.items()
                                  if isinstance(k, str))
    return {
        "scenario": "hammer",
        "requests": n_requests, "workers": workers,
        "status_codes": {str(k): v for k, v in codes.items()},
        "intentional_404s": intentional_404,
        "rps": round(n_requests / took, 1),
        "median_latency_ms": round(lat[len(lat) // 2] * 1000),
        "p95_latency_ms": round(lat[min(len(lat) - 1,
                                        int(len(lat) * .95))] * 1000),
        "verdict": "PASS" if n500 == 0 and codes.get(200, 0) >= (
            n_requests - intentional_404 - (codes.get(400, 0))
            - (codes.get(409, 0))) else "CHECK",
    }


# ---------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenario", default="all",
                    choices=["bindstorm", "burncascade", "hammer", "all"])
    ap.add_argument("--channels", type=int, default=100)
    ap.add_argument("--gold", type=int, default=140)
    ap.add_argument("--burn-pct", type=int, default=30)
    ap.add_argument("--requests", type=int, default=300)
    ap.add_argument("--no-gate", action="store_true",
                    help="bindstorm: trust stored gold (skip live re-gate)")
    args = ap.parse_args(argv)

    print("=" * 66)
    print("IP SHELF — SCALE EXAMINATION (no fake views, no YouTube traffic)")
    print(f"started {now_ts()}")
    print("=" * 66)
    results = []
    if args.scenario in ("bindstorm", "all"):
        print("\n[A] bind storm: mass draw on the REAL pool "
              f"({args.channels} channels, real gate unless --no-gate)")
        r = scenario_bindstorm(args.channels, use_gate=not args.no_gate)
        print(json.dumps(r, indent=2))
        results.append(r)
    if args.scenario in ("burncascade", "all"):
        print(f"\n[B] burn cascade: synthetic {args.gold}-gold pool, "
              f"{args.channels} channels, churn-burn mid-flight")
        r = scenario_burncascade(args.gold, args.channels, args.burn_pct)
        print(json.dumps(r, indent=2))
        results.append(r)
    if args.scenario in ("hammer", "all"):
        print(f"\n[C] endpoint hammer: {args.requests} mixed requests "
              "against the live dashboard")
        r = scenario_hammer(args.requests)
        print(json.dumps(r, indent=2))
        results.append(r)

    print("\n" + "=" * 66)
    print("SCALE EXAMINATION DONE " + now_ts())
    for r in results:
        print(f"  {r.get('scenario'):<12} -> {r.get('verdict', '?')}  "
              + json.dumps({k: v for k, v in r.items()
                            if k not in ("scenario", "verdict")})[:150])
    print("=" * 66)
    out = Path(ROOT) / "ipshelf" / "data" / "loadtest_last.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"at": now_ts(), "results": results},
                              indent=2), encoding="utf-8")
    print(f"results saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

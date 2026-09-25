"""gold_factory.py — Factory #1: continuous gold-IP harvest daemon.

THE DESIGN (replaces batch harvest):
  every cycle (default 5 min):
    1. SCRAPE  all 30+ proxy sources in parallel
    2. DEDUPE  against data/factory_seen.json — only NEW proxies get tested
    3. TEST    new candidates with the playability oracle (gate.quick_gold_check:
               35s cap + gzip — the fixed checker), high parallelism
    4. CERTIFY gold = playabilityStatus OK; geo + score each one
    5. MERGE   into the shelf pool (ipshelf/data/shelf_pool.json)
    6. SHIP    rebuild data/ip_pool_snapshot.json (T1-first) and git
               commit+push IF new gold landed — the watcher picks it up
               from the repo on its next run
    7. DISPATCH  optionally fire a watcher run right away (--dispatch N)

Because only the NEW delta is tested each cycle, a cycle takes minutes,
not an hour — gold is minutes old when it ships, not 40.

Fleet mode (GitHub Actions): run with --once --shard i --shards N --no-ship;
each runner tests its slice and prints gold as JSON to stdout (the workflow
uploads it as an artifact for factory_collect.py to merge).

Usage:
  python gold_factory.py --ship                 # local daemon, auto-ship
  python gold_factory.py --once --max-test 500  # single small cycle
  python gold_factory.py --once --shard 0 --shards 10 --no-ship   # CI shard
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
SEEN_FILE = DATA / "factory_seen.json"
GOLD_LOG = DATA / "factory_gold_log.jsonl"
SNAPSHOT = DATA / "ip_pool_snapshot.json"

from ipshelf.core import gate, shelf                     # noqa: E402
from ipshelf.hunt.exitpool import geo_lookup2, ip_type, score_exit, tier_of  # noqa: E402

# (url, default proto) — same sources as proxy_hunt.sh
SOURCES = [
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "HTTP"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "HTTP"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "SOCKS"),
    ("https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all", "HTTP"),
    ("https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all", "SOCKS"),
    ("https://openproxylist.xyz/http.txt", "HTTP"),
    ("https://openproxylist.xyz/socks5.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt", "HTTP"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt", "AUTO"),
    ("https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt", "HTTP"),
    ("https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks5.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/http.txt", "HTTP"),
    ("https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/socks5.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/http.txt", "HTTP"),
    ("https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt", "HTTP"),
    ("https://raw.githubusercontent.com/dpangestuw/Free-PROXY/main/http_proxies.txt", "HTTP"),
    ("https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt", "HTTP"),
    ("https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt", "HTTP"),
    ("https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/socks5.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt", "HTTP"),
    ("https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks5.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt", "HTTP"),
    ("https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt", "HTTP"),
    ("https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt", "SOCKS"),
    ("https://raw.githubusercontent.com/mauricegift/free-proxies/master/files/http.json", "HTTP"),
    ("https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/http-proxy-list-by-EbraSha.txt", "HTTP"),
    ("https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/socks5-proxy-list-by-EbraSha.txt", "SOCKS"),
]

ADDR_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})")
PROTO_RE = re.compile(r"^(socks4|socks5|https?)://", re.I)

T1 = {"US", "GB", "CA", "DE", "FR", "NL", "SE", "CH", "AT", "DK", "NO",
      "FI", "IE", "BE", "LU", "JP", "SG", "AU", "NZ", "KR", "HK", "TW"}


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ scrape

def _fetch(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def scrape_sources():
    """Download every source in parallel -> {(addr, proto): 1}."""
    found = {}
    results_lock = threading.Lock()

    def _scrape_one(src):
        url, default_proto = src
        body = _fetch(url)
        if not body:
            return 0
        n = 0
        for line in body.splitlines():
            m = ADDR_RE.search(line)
            if not m:
                continue
            addr = f"{m.group(1)}:{m.group(2)}"
            pm = PROTO_RE.match(line.strip())
            proto = (pm.group(1).upper() if pm else default_proto)
            if proto in ("HTTPS", "AUTO"):
                proto = "HTTP"
            if proto == "SOCKS4":
                proto = "SOCKS"   # gate speaks socks5; socks4 yield is tiny
            with results_lock:
                found[(addr, proto)] = 1
            n += 1
        return n

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = {ex.submit(_scrape_one, s): s for s in SOURCES}
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
            except Exception:
                pass
    return found


# ------------------------------------------------------------------- state

def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_seen(seen):
    # prune entries older than 7 days to bound growth
    cutoff = time.time() - 7 * 86400
    pruned = {k: v for k, v in seen.items()
              if isinstance(v, dict) and v.get("ts", 0) > cutoff}
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(pruned), encoding="utf-8")
    os.replace(tmp, SEEN_FILE)


# -------------------------------------------------------------------- test

def test_candidates(cands, par):
    """gate.quick_gold_check over candidates. Returns list of gold entries."""
    golds = []

    def _one(c):
        addr, proto = c
        try:
            r = gate.quick_gold_check(addr, proto, timeout=35)
            if r.get("ok"):
                return {"addr": addr, "proto": proto,
                        "latency_ms": r.get("latency_ms"),
                        "egress_ip": r.get("egress_ip")}
        except Exception:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=par) as ex:
        for res in ex.map(_one, cands):
            if res:
                golds.append(res)
    return golds


def enrich(golds):
    """Geo + tier + score for certified gold (batch ip-api)."""
    if not golds:
        return []
    ipmap = geo_lookup2([g.get("egress_ip") for g in golds])
    for g in golds:
        geo = ipmap.get(g.get("egress_ip"), {})
        g["cc"] = geo.get("countryCode", "??")
        g["tier"] = tier_of(g["cc"])
        g["type"] = ip_type(geo)
        g["score"] = score_exit({**g, "bandwidth_mbps": 0,
                                 "playability": "OK", "github": 0})
        g["first_seen"] = shelf.now_ts()
    golds.sort(key=lambda g: (0 if g.get("cc") in T1 else 1, -g["score"]))
    return golds


# ------------------------------------------------------------------- ship

def rebuild_snapshot():
    """Snapshot = all shelf gold, T1-first then score — the watcher's menu."""
    pool = shelf.load_pool()
    golds = [e for e in pool.get("exits", []) if e.get("status") == "gold"]
    golds.sort(key=lambda e: (0 if e.get("cc") in T1 else 1, -(e.get("score") or 0)))
    snap = {"snapshotted_at": shelf.now_ts(), "exits": golds}
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    os.replace(tmp, SNAPSHOT)
    return len(golds)


def ship(new_gold_n):
    n = rebuild_snapshot()
    changed = subprocess.run(["git", "status", "--porcelain", "data/ip_pool_snapshot.json"],
                             cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    if not changed:
        log(f"[ship] snapshot unchanged ({n} gold) — nothing to push")
        return False
    subprocess.run(["git", "add", "data/ip_pool_snapshot.json"], cwd=str(ROOT), check=True)
    msg = f"gold-factory: +{new_gold_n} fresh gold, snapshot {n} exits [skip-ci]"
    subprocess.run(["git", "commit", "-m", msg], cwd=str(ROOT), check=True)
    r = subprocess.run(["git", "push"], cwd=str(ROOT), capture_output=True, text=True, timeout=90)
    if r.returncode == 0:
        log(f"[ship] PUSHED: {msg}")
        return True
    log(f"[ship] push failed: {r.stderr.strip()[:200]}")
    return False


def dispatch(tabs):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "Zarifbenalam-2/yt-ghost-runner")
    if not token:
        log("[dispatch] no GITHUB_TOKEN env — skipped")
        return
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"cloud_ghost_watch.yml/dispatches")
    body = json.dumps({"ref": "main", "inputs": {
        "video_url": os.environ.get("FACTORY_VIDEO_URL",
                                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        "tabs_per_runner": str(tabs), "use_ip_pool": "true"}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json", "User-Agent": "gold-factory"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"[dispatch] watcher fired (HTTP {r.status}) tabs_per_runner={tabs}")
    except Exception as e:
        log(f"[dispatch] failed: {e}")


def append_gold_log(golds):
    if not golds:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    with open(GOLD_LOG, "a", encoding="utf-8") as f:
        for g in golds:
            f.write(json.dumps({**g, "certified_at": shelf.now_ts()}) + "\n")


# ------------------------------------------------------------------- cycle

def run_cycle(args, seen):
    log("== CYCLE: scrape ==")
    found = scrape_sources()
    log(f"scraped {len(found)} unique (addr,proto) from {len(SOURCES)} sources")

    fresh = [k for k in found if k[0] not in seen]
    log(f"new (never seen): {len(fresh)}")

    import random
    random.shuffle(fresh)   # uniform coverage across cycles

    if args.shards > 1:
        fresh = sorted(fresh)
        fresh = fresh[args.shard::args.shards]
        log(f"shard {args.shard}/{args.shards}: testing slice of {len(fresh)}")

    if not fresh:
        return 0

    if len(fresh) > args.max_test:
        # T1 lists first when we can't test all; else take from the front
        fresh = fresh[:args.max_test]
        log(f"capped testing to {args.max_test} this cycle")

    t0 = time.time()
    log(f"== CYCLE: testing {len(fresh)} at par={args.par} ==")
    golds = test_candidates(fresh, args.par)
    dt = round(time.time() - t0, 1)
    log(f"tested {len(fresh)} in {dt}s -> {len(golds)} GOLD")

    golds = enrich(golds)
    for g in golds:
        log(f"  GOLD {g['addr']:24s} {g.get('cc','??'):3s} T{g.get('tier','-')[1] if g.get('tier') else '?'} "
            f"score={g.get('score')} {g.get('type','?')}")

    # record seen regardless of outcome (dead now = seen; lists re-add later
    # only if pruned after 7 days — a retest pass can revisit)
    now = time.time()
    for addr, proto in fresh:
        seen[addr] = {"ts": now, "proto": proto}

    if golds and args.shards == 1:
        pool = shelf.load_pool()
        res = shelf.merge_exits(pool, golds)
        shelf.save_pool(pool)
        append_gold_log(golds)
        log(f"shelf merge: +{res['added']} added, {res['refreshed']} refreshed")
        if args.ship and res["added"]:
            ship(res["added"])
            if args.dispatch:
                dispatch(args.dispatch)
        return res["added"]

    if golds:
        print("FACTORY_GOLD_JSON=" + json.dumps(golds), flush=True)
        if args.shards > 1:
            # fleet mode: drop gold as a file for the workflow artifact upload
            DATA.mkdir(parents=True, exist_ok=True)
            (DATA / f"factory_gold_shard_{args.shard}.json").write_text(
                json.dumps(golds, indent=2), encoding="utf-8")
    return len(golds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, default=300, help="seconds between cycles")
    ap.add_argument("--par", type=int, default=500, help="parallel gold checks")
    ap.add_argument("--max-test", type=int, default=8000, help="max new IPs tested per cycle")
    ap.add_argument("--ship", action="store_true", help="auto commit+push snapshot on new gold")
    ap.add_argument("--dispatch", type=int, default=0, help="auto-fire watcher with N tabs/runner after ship")
    ap.add_argument("--once", action="store_true", help="single cycle then exit")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--no-ship", dest="ship", action="store_false")
    args = ap.parse_args()

    seen = load_seen()
    log(f"GOLD FACTORY start: cycle={args.cycle}s par={args.par} max_test={args.max_test} "
        f"ship={args.ship} shard={args.shard}/{args.shards} seen={len(seen)}")

    while True:
        try:
            run_cycle(args, seen)
        except Exception as e:
            log(f"cycle error: {type(e).__name__}: {e}")
        save_seen(seen)
        if args.once:
            break
        time.sleep(args.cycle)


if __name__ == "__main__":
    main()

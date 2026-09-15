#!/usr/bin/env python3
# ipshelf/hunt — self-contained copy of the gold hunt machine (no dependency
# on FRee Gold IP). Adapted: docstring state path made HERE-relative; Windows
# CURL detection + all grading logic unchanged.
"""ExitPool — free proxy exit intelligence & scoring system.

The problem we solve:
  Public free proxies are a race. An exit is listed -> thousands of automation
  users find it -> Google's bot sensors see anomalous traffic -> the exit's IP
  reputation gets flagged (LOGIN_REQUIRED / sorry-wall) within 1-3 hours.
  Value = freshness + quality. The winner harvests faster and MEASURES what
  they grab: geo, tier, IP type, latency, bandwidth, playability, survival.

Commands:
  grade     Benchmark the GOLD exits (from proxyhunt rank_results.txt):
            egress IP -> geo (ip-api) -> tier -> type -> latency ->
            bandwidth -> YouTube playability -> GitHub access -> score.
  retest    Survival check: re-run page+playability for pool entries.
  top       Print the ranked pool table.

State: <this dir>/proxyhunt/pool.json (HERE-relative, portable)
Design: zero dependencies (stdlib + curl subprocess), thread-parallel.
"""

import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

# portable: live next to proxy_hunt.sh output, wherever this file lives
HUNT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxyhunt")
POOL_PATH = f"{HUNT_DIR}/pool.json"
RANK_PATH = f"{HUNT_DIR}/rank_results.txt"

WATCH_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
LAT_URL = "https://www.gstatic.com/generate_204"
EGRESS_URL = "https://api.ipify.org"
BW_URL = "https://speed.cloudflare.com/__down?bytes=8000000"
GH_URL = "https://api.github.com/zen"

T1 = {"US", "GB", "CA", "DE", "FR", "NL", "SE", "CH", "AT", "DK", "NO", "FI",
      "IE", "BE", "LU", "JP", "SG", "AU", "NZ", "KR", "HK", "TW"}
T2 = {"ES", "IT", "PT", "PL", "CZ", "SK", "HU", "RO", "BG", "GR", "HR", "SI",
      "EE", "LV", "LT", "IL", "AE", "QA", "SA", "TR", "MY", "TH", "VN", "IN",
      "ID", "PH", "BR", "MX", "AR", "CL", "CO", "ZA", "RU", "UA"}

DC_HINTS = ("ovh", "hetzner", "digitalocean", "linode", "vultr", "oracle",
            "amazon", "aws", "google llc", "microsoft", "azure", "contabo",
            "choopa", "vultr", "leaseweb", "datacamp", "scaleway", "hostinger",
            "cloud", "server", "hosting", "colo", "datacenter", "vps",
            "m247", "gcore", "g-core", "packethub", "psyzon", "ipxo", "ipgel")


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Windows portability: prefer Git-Bash/MSYS curl (handles /dev/null, socks
# via subprocess) over C:\Windows\System32\curl.exe. On Linux/macOS this
# resolves to the same "curl" PATH hit as before.
CURL = shutil.which("curl")
if CURL and "WindowsApps" in CURL:
    CURL = None
for cand in (os.environ.get("CURL_BIN"),
             r"C:\Program Files\Git\mingw64\bin\curl.exe",
             "/usr/bin/curl", "/mingw64/bin/curl"):
    if cand and os.path.isfile(cand):
        CURL = cand
        break
if not CURL:
    print("[exitpool] WARNING: no usable curl found (install Git for Windows or set CURL_BIN)",
          file=sys.stderr)


def curl(url, proxy=None, socks=False, max_time=15, extra=None):
    """Run curl, return (http_code, ttfb, ttotal, size)."""
    cmd = [CURL, "-s", "-o", os.devnull, "-w",
           "%{http_code} %{time_starttransfer} %{time_total} %{size_download}",
           "--max-time", str(max_time)]
    if proxy:
        if socks:
            cmd += ["--socks5-hostname", proxy]
        else:
            cmd += ["-x", f"http://{proxy}"]
    if extra:
        cmd += extra
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=max_time + 10).stdout
        parts = out.split()
        code = parts[0] if parts else "000"
        ttfb = float(parts[1]) if len(parts) > 1 else 0
        ttotal = float(parts[2]) if len(parts) > 2 else 0
        size = float(parts[3]) if len(parts) > 3 else 0
        return code, ttfb, ttotal, size
    except Exception:
        return "000", 0, 0, 0


def curl_body(url, proxy=None, socks=False, max_time=20):
    """Fetch body text through a proxy."""
    cmd = [CURL, "-s", "--max-time", str(max_time)]
    if proxy:
        if socks:
            cmd += ["--socks5-hostname", proxy]
        else:
            cmd += ["-x", f"http://{proxy}"]
    cmd.append(url)
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=max_time + 10).stdout
    except Exception:
        return ""


def tier_of(cc):
    return "T1" if cc in T1 else ("T2" if cc in T2 else "T3")


def ip_type(geo):
    """Classify exit: mobile / residential / datacenter (heuristics)."""
    if geo.get("mobile"):
        return "mobile"
    blob = " ".join(str(geo.get(k, "")).lower() for k in ("isp", "org", "as"))
    if geo.get("hosting") or any(h in blob for h in DC_HINTS):
        return "datacenter"
    if geo.get("proxy"):
        return "proxy-flagged"
    return "residential"


def geo_lookup(ips):
    """Batch geo lookup via ip-api.com (free). Returns {ip: geo_dict}."""
    ips = [i for i in ips if i]
    if not ips:
        return {}
    return geo_lookup2(ips)


def geo_lookup2(ips):
    """Batch geo lookup via ip-api.com (free, HTTP, batch of 100)."""
    ips = [i for i in ips if i][:95]
    if not ips:
        return {}
    out = {}
    try:
        import urllib.request
        body = json.dumps(ips).encode()
        url = ("http://ip-api.com/batch?fields=status,country,countryCode,region,"
               "city,isp,org,as,mobile,proxy,hosting,query")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            for row in json.loads(resp.read().decode() or "[]"):
                if row.get("status") == "success":
                    out[row["query"]] = row
    except Exception as exc:
        print(f"[!] geo lookup failed: {exc}", file=sys.stderr)
    return out


def grade_exit(entry):
    """Full benchmark of one exit. entry = {addr, proto, first_seen}."""
    addr, proto = entry["addr"], entry["proto"]
    socks = proto == "SOCKS"
    res = dict(entry)

    # 1) egress IP (true public IP as seen by the internet)
    body = curl_body(EGRESS_URL, addr, socks, 12)
    res["egress_ip"] = body.strip() if re.match(r"^\d+\.\d+\.\d+\.\d+$", body.strip() or "x") else ""

    # 2) latency on Google infra (gstatic 204)
    code, ttfb, _, _ = curl(LAT_URL, addr, socks, 12)
    res["latency_ms"] = round(ttfb * 1000) if code in ("204", "200") else None
    res["gstatic_code"] = code

    # 3) YouTube watch page + playability
    html = curl_body(WATCH_URL, addr, socks, 18)
    if html:
        m = re.search(r'"playabilityStatus":\s*\{"status":\s*"([A-Z_]+)"', html)
        res["playability"] = m.group(1) if m else "NO_STATUS"
        res["page_ok"] = len(html) > 50000  # real watch pages are huge
    else:
        res["playability"], res["page_ok"] = "DEAD", False

    # 4) bandwidth: 8MB through Cloudflare speed endpoint
    code, _, ttotal, size = curl(BW_URL, addr, socks, 30)
    res["bw_code"] = code
    res["bandwidth_mbps"] = round(size * 8 / ttotal / 1e6, 2) if ttotal > 0 and size > 100000 else 0

    # 5) GitHub API reachability (their second block)
    gh_code, _, _, _ = curl(GH_URL, addr, socks, 12)
    res["github"] = "OK" if gh_code == "200" else ("RATELIMIT" if gh_code == "403" else f"FAIL({gh_code})")

    res["last_checked"] = now_ts()
    return res


def score_exit(e):
    """Composite 0-100. Gate: playability OK, else heavily penalized."""
    s = 0
    play = e.get("playability")
    if play == "OK":
        s += 25
    elif play in ("LOGIN_REQUIRED",):
        s += 8
    # bandwidth up to 25 (5 Mbps saturates)
    s += min(e.get("bandwidth_mbps", 0), 5) / 5 * 25
    # latency up to 20 (300ms full, 3s+ zero)
    lat = e.get("latency_ms")
    if lat:
        s += max(0, min((3000 - lat) / 2700, 1)) * 20
    # tier up to 15
    s += {"T1": 15, "T2": 8, "T3": 3}.get(e.get("tier", "T3"), 3)
    # ip type up to 10
    s += {"residential": 10, "mobile": 8, "proxy-flagged": 4, "datacenter": 2}.get(e.get("type", ""), 2)
    # github bonus 5
    s += 5 if e.get("github") == "OK" else 0
    return round(s, 1)


def load_gold():
    """Load GOLD exits from rank_results.txt."""
    entries = []
    if not os.path.isfile(RANK_PATH):
        return entries
    seen = set()
    for line in open(RANK_PATH):
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0] == "OK":
            proto, addr = parts[1], parts[2]
            if addr not in seen:
                seen.add(addr)
                entries.append({"addr": addr, "proto": proto,
                                "first_seen": now_ts()})
    return entries


def cmd_grade():
    gold = load_gold()
    print(f"[*] {len(gold)} GOLD exits loaded; benchmarking (latency/bw/geo/yt/gh)...")
    results = []
    with cf.ThreadPoolExecutor(max_workers=30) as ex:
        futs = {ex.submit(grade_exit, e): e for e in gold}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            try:
                r = fut.result()
                results.append(r)
                print(f"    [{i:2d}/{len(gold)}] {r['proto']:5s} {r['addr']:24s} "
                      f"play={r.get('playability'):14s} lat={str(r.get('latency_ms')):>6s}ms "
                      f"bw={str(r.get('bandwidth_mbps')):>6s}Mbps gh={r.get('github')}")
            except Exception as exc:
                print(f"    [!] grade error: {exc}")

    # geo enrichment (batch, direct)
    print("[*] geo-enriching egress IPs via ip-api.com ...")
    ipmap = geo_lookup2([r.get("egress_ip") for r in results])
    for r in results:
        g = ipmap.get(r.get("egress_ip"), {})
        if g:
            r["geo"] = {k: g.get(k) for k in
                        ("country", "countryCode", "region", "city", "isp", "org", "as")}
            r["cc"] = g.get("countryCode", "??")
            r["tier"] = tier_of(r["cc"])
            r["type"] = ip_type(g)
        else:
            r["geo"], r["cc"], r["tier"], r["type"] = {}, "??", "T3", "unknown"
        r["score"] = score_exit(r)

    results.sort(key=lambda r: -r["score"])
    with open(POOL_PATH, "w") as f:
        json.dump({"graded_at": now_ts(), "exits": results}, f, indent=2)
    print(f"[*] pool written: {POOL_PATH}")
    print_top(results)
    return results


def retest_exit(e):
    addr, socks = e["addr"], e["proto"] == "SOCKS"
    html = curl_body(WATCH_URL, addr, socks, 15)
    m = re.search(r'"playabilityStatus":\s*\{"status":\s*"([A-Z_]+)"', html or "")
    e2 = dict(e)
    e2["retest_playability"] = m.group(1) if m else ("DEAD" if not html else "NO_STATUS")
    e2["retest_at"] = now_ts()
    return e2


def cmd_retest():
    if not os.path.isfile(POOL_PATH):
        print(f"[!] pool not found: {POOL_PATH} — run 'grade' first")
        return
    pool = json.load(open(POOL_PATH))
    exits = pool["exits"]
    print(f"[*] survival re-test of {len(exits)} exits (first graded {pool['graded_at']})...")
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        out = list(ex.map(retest_exit, exits))
    alive = [e for e in out if e["retest_playability"] == "OK"]
    for e in out:
        print(f"    {e['proto']:5s} {e['addr']:24s} {e.get('cc','??'):2s} "
              f"score={e.get('score'):5} now={e['retest_playability']}")
    pool["retested_at"] = now_ts()
    pool["survivors"] = len(alive)
    pool["exits"] = out
    json.dump(pool, open(POOL_PATH, "w"), indent=2)
    print(f"[*] survivors: {len(alive)}/{len(exits)}")


def print_top(exits):
    print("\n RANK PROTO ADDR                CC TIER TYPE         SCORE LAT(ms) BW(Mbps) GH")
    print("-" * 92)
    for i, e in enumerate(exits[:25], 1):
        print(f" {i:4d} {e['proto']:5s} {e['addr']:21s} {e.get('cc','??'):2s} "
              f"{e.get('tier','?'):3s} {e.get('type','?'):12s} {e.get('score',0):5} "
              f"{str(e.get('latency_ms')):>6} {str(e.get('bandwidth_mbps')):>8} {e.get('github')}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"
    if cmd == "grade":
        cmd_grade()
    elif cmd == "retest":
        cmd_retest()
    elif cmd == "top":
        if not os.path.isfile(POOL_PATH):
            print(f"[!] pool not found: {POOL_PATH} — run 'grade' first")
            return
        pool = json.load(open(POOL_PATH))
        print_top(pool["exits"])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

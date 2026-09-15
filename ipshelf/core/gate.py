"""gate.py — single-IP quick gold check + proxy URL builder.

THE LAWS behind this module:

  LAW 4  Only graded gold counts. "Gold" is not a guess: the proxy must
         fetch the REAL YouTube watch page and get
         "playabilityStatus": {"status": "OK"}. YouTube's playability
         oracle is a live IP-reputation check — if the exit is flagged,
         it answers LOGIN_REQUIRED / ERROR instead of OK.
  LAW 6  Quick gold gate before bind. Every IP drawn from a shelf is
         re-gated right before binding; only a pass binds. Same for a
         sticky IP reused next day.
  LAW 8  Next-day reuse: same channel, same sticky IP, re-gated — pass
         keeps it, fail burns it (LAW 5).

Design: zero dependencies — curl subprocess only (pattern lifted from
the hunt machine's exitpool.py). On Windows we prefer Git-Bash/MSYS
curl (mingw64) over C:\\Windows\\System32\\curl.exe because the MSYS
build handles --socks5-hostname properly in a subprocess.
"""

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from . import shelf

WATCH_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
LAT_URL = "https://www.gstatic.com/generate_204"
EGRESS_URL = "https://api.ipify.org"

# ---------------------------------------------------------------------------
# curl detection (same strategy as exitpool.py, Windows-safe):
#   1. PATH hit via shutil.which, but skip the MS Store WindowsApps stub
#   2. CURL_BIN env var
#   3. common Git-for-Windows install paths
# ---------------------------------------------------------------------------

def _find_curl() -> str:
    curl = shutil.which("curl")
    if curl and "WindowsApps" in curl:
        curl = None  # MS Store python/curl stub — useless
    for cand in (os.environ.get("CURL_BIN"),
                 r"C:\Program Files\Git\mingw64\bin\curl.exe",
                 r"C:\Program Files (x86)\Git\mingw64\bin\curl.exe",
                 "/usr/bin/curl", "/mingw64/bin/curl"):
        if cand and os.path.isfile(cand):
            curl = cand
            break
    return curl


CURL = _find_curl()

# --------------------------------------------------------------------- URLs

def proxy_url(entry) -> str:
    """Canonical proxy URL for an exit entry: socks5:// or http://."""
    addr = entry["addr"]
    if entry.get("proto", "").upper() == "SOCKS":
        return f"socks5://{addr}"
    return f"http://{addr}"


def proxy_url_variants(entry) -> list:
    """All URL forms a browser may need to try for this exit.

    Many proxies listed as "socks5" are actually socks4 — Chromium's
    --proxy-server accepts both schemes, so for SOCKS exits we offer
    both; for HTTP exits there is a single form.
    """
    addr = entry["addr"]
    if entry.get("proto", "").upper() == "SOCKS":
        return [f"socks5://{addr}", f"socks4://{addr}"]
    return [f"http://{addr}"]


# ------------------------------------------------------------------- curl IO

def _curl_stats(url, proxy=None, socks=False, max_time=15):
    """Run curl; return (http_code, ttfb, ttotal, size)."""
    if not CURL:
        return "000", 0.0, 0.0, 0.0
    cmd = [CURL, "-s", "-o", os.devnull, "-w",
           "%{http_code} %{time_starttransfer} %{time_total} %{size_download}",
           "--max-time", str(max_time)]
    if proxy:
        if socks:
            cmd += ["--socks5-hostname", proxy]
        else:
            cmd += ["-x", f"http://{proxy}"]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=max_time + 10).stdout
        parts = out.split()
        code = parts[0] if parts else "000"
        ttfb = float(parts[1]) if len(parts) > 1 else 0.0
        ttotal = float(parts[2]) if len(parts) > 2 else 0.0
        size = float(parts[3]) if len(parts) > 3 else 0.0
        return code, ttfb, ttotal, size
    except Exception:
        return "000", 0.0, 0.0, 0.0


def _curl_body(url, proxy=None, socks=False, max_time=20) -> str:
    """Fetch body text through a proxy (empty string on failure)."""
    if not CURL:
        return ""
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


# --------------------------------------------------------------------- gate

def quick_gold_check(addr: str, proto: str, timeout: int = 18) -> dict:
    """Quick gold gate for one proxy address.

    Steps (each bounded by ~timeout seconds):
      1. gstatic generate_204  -> latency (code MUST be 204)
      2. api.ipify.org         -> egress IP
      3. YouTube watch page    -> regex "playabilityStatus": {"status": "X"

    ok=True ONLY when playability == "OK" (LAW 4).

    Returns {"ok", "playability", "latency_ms", "egress_ip", "checked_at"}.
    Also appends one JSON line to ipshelf/data/ipshelf.log.
    """
    socks = str(proto).upper() == "SOCKS"
    checked_at = shelf.now_ts()

    # 1) latency on Google infra — must come back 204
    code, ttfb, _t, _s = _curl_stats(LAT_URL, addr, socks, timeout)
    latency_ms = round(ttfb * 1000) if code == "204" else None

    # 2) egress IP (true public IP as the internet sees it)
    body = _curl_body(EGRESS_URL, addr, socks, timeout)
    body = (body or "").strip()
    egress_ip = body if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", body) else ""

    # 3) YouTube watch page + playability oracle
    html = _curl_body(WATCH_URL, addr, socks, timeout)
    if html:
        m = re.search(r'"playabilityStatus":\s*\{"status":\s*"([A-Z_]+)"', html)
        playability = m.group(1) if m else "NO_STATUS"
    else:
        playability = "DEAD"

    ok = playability == "OK"
    result = {"ok": ok, "playability": playability,
              "latency_ms": latency_ms, "egress_ip": egress_ip,
              "checked_at": checked_at}
    shelf.append_log("gate", addr=addr, ok=ok, playability=playability)
    return result


def gate_many(entries, max_workers: int = 10) -> list:
    """Gate a list of exit entries in parallel.

    Returns a list of result dicts aligned with the input order
    (ThreadPoolExecutor.map preserves order).
    """
    def _one(e):
        try:
            return quick_gold_check(e["addr"], e.get("proto", "HTTP"))
        except Exception as exc:  # never let one bad entry kill the batch
            return {"ok": False, "playability": f"ERROR({exc})",
                    "latency_ms": None, "egress_ip": "",
                    "checked_at": shelf.now_ts()}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_one, entries))

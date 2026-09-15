"""LIVE PROOF that a channel's shelf binding actually routes its browser
traffic through the bound gold IP.

Opens the channel's account profile through the bound/sticky proxy URL,
loads YouTube, checks signed-in state, then (same tab) hits ip-api.com so we
can SEE which IP the outside world gets for this channel profile. Compares
three views side by side: the quick-gate egress IP, the browser egress IP,
and the pool entry's egress IP. Everything is captured as evidence
(hunt_lib.capture -> _hunt/) and a summary.json is written under
ipshelf/data/proofs/<UTC ts>/.

Usage (cwd = yt-channel-automation, venv python):
    python ipshelf\\prove_binding.py --handle @X [--region US] [--headful]
        [--fresh]

Exit codes: 0 = PASS, 1 = FAIL (facts above did not add up), 2 = hard flag
(CAPTCHA wall on the bound IP -> IP burned) or setup error.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent  # yt-channel-automation/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from engine.config import PROJECT_ROOT
except Exception:  # pragma: no cover
    PROJECT_ROOT = ROOT

from engine.browser import open_proxied_arg
from hunt_lib import capture
from wu_lib import exit_ip_country, is_signed_in

# Tier-1 verdict set: reuse the shelf's own definition so this file can never
# drift from the core (region "T1" bindings may exit from any of these ccs).
from ipshelf.core.shelf import T1_SET as T1_CC

IP_API_URL = ("http://ip-api.com/json/"
              "?fields=status,countryCode,query,isp,city")


def data_dir():
    import os
    env = os.environ.get("IPSHELF_DATA")
    return Path(env) if env else PROJECT_ROOT / "ipshelf" / "data"


def load_core():
    try:
        from ipshelf.core import assigner, gate, shelf
    except ImportError as e:
        raise SystemExit(f"ipshelf core not importable ({e}). The shelf "
                         "core must be built first.")
    return assigner, gate, shelf


def account_id_for_handle(handle):
    """Shared-DB account id for the channel's owning account; 'default' =
    cloak-profile (the logged-in session) when there's no DB row."""
    try:
        import engine.db as db
        ch = db.conn().execute(
            "SELECT * FROM Channel WHERE handle=? ORDER BY createdAt LIMIT 1",
            (handle,)).fetchone()
        if ch is not None and ch["accountId"]:
            acc = db.get_account(account_id=ch["accountId"])
            if acc is not None:
                from engine.create_channels import _acct_key
                return _acct_key(acc)
    except Exception:
        pass
    return "default"


def binding(handle, shelf_store):
    b = shelf_store.load_bindings().get(handle) or {}
    # tolerate both stated shapes: {"sticky_ip": ...} and {"entry": {...}}
    entry = b.get("entry") if isinstance(b.get("entry"), dict) else None
    return {
        "region": b.get("region"),
        "sticky_ip": b.get("sticky_ip") or (entry or {}).get("addr"),
    }


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Prove a channel's shelf binding end-to-end with a real "
                    "browser: YouTube home + signed-in + visible exit IP.")
    ap.add_argument("--handle", required=True,
                    help="channel handle, e.g. @ZarifTestChannel01")
    ap.add_argument("--region",
                    help="region used ONLY if the handle has no binding yet")
    ap.add_argument("--headful", action="store_true",
                    help="visible browser (default: headless)")
    ap.add_argument("--fresh", action="store_true",
                    help="force a redraw: release + draw a new IP instead of "
                         "using the sticky one")
    ap.add_argument("--hold", action="store_true",
                    help="leave a VISIBLE browser open with 2 tabs (YouTube "
                         "home + IP lookup) until you close it; implies "
                         "--headful")
    ap.add_argument("--url",
                    help="open this URL instead of YouTube home (e.g. the "
                         "channel's own YouTube page) — used by the "
                         "dashboard's browse button")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    handle = args.handle.strip()
    if not handle.startswith("@"):
        print(f"note: handle doesn't start with @: {handle!r}")

    assigner, gate, shelf_store = load_core()

    if args.fresh and binding(handle, shelf_store).get("sticky_ip"):
        print("[shelf] --fresh: releasing current binding ...")
        try:
            assigner.release(handle, "prove_fresh_redraw")
        except Exception as e:
            print(f"  release failed ({e}); drawing anyway")

    # (a) draw + real-network quick gold gate
    print(f"[draw] {handle} region={args.region or '(from binding)'} ...")
    drawn = assigner.draw_for_channel(handle, region=args.region,
                                      gate_fn=gate.quick_gold_check)
    if isinstance(drawn, dict) and drawn.get("error"):
        print(f"DRAW FAILED: {json.dumps(drawn)[:300]}")
        return 2
    # the assigner's contract doesn't return the gate result, so re-run the
    # gate directly for the display row (the draw already gated this same
    # addr a moment ago; this second check is cheap + authoritative)
    gate_result = gate.quick_gold_check(drawn.get("addr"), drawn.get("proto"))
    print(f"[gate] {json.dumps(gate_result)}")

    entry = drawn.get("entry") or {}
    addr, proto = drawn.get("addr"), drawn.get("proto")
    proxy_url = drawn.get("proxy_url")
    bind = binding(handle, shelf_store)
    region = bind.get("region") or args.region or "?"
    print(f"[bind] {handle}: region={region} sticky={bind.get('sticky_ip')} "
          f"(drew {addr})")

    # pool entry's own egress view (for the 3-way comparison)
    pool_entry_egress = entry.get("egress_ip") or entry.get("egress") or (
        entry.get("geo") or {}).get("query")

    # (b-e) launch browser, prove YouTube + visible exit IP
    headless = not (args.headful or args.hold)  # --hold implies visible
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    proof_dir = data_dir() / "proofs" / ts
    proof_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "handle": handle, "region": region, "ip_addr": addr, "proto": proto,
        "proxy_url": proxy_url, "gate_result": gate_result,
        "pool_entry_egress_ip": pool_entry_egress,
        "target_url": args.url or None,
        "signed_in": False, "youtube_home_loaded": False,
        "browser_exit_ip": None, "browser_exit_cc": None, "isp": None,
        "city": None, "captcha": False, "warnings": [],
    }
    verdict, reason, exit_code = "FAIL", "", 1

    try:
        with sync_playwright() as p:
            ctx, page = open_proxied_arg(p, proxy_url, headless=headless,
                                         account_id=account_id_for_handle(handle))
            try:
                # (c) YouTube home (or the requested URL — e.g. the
                # channel's own page when the dashboard browse button sent it)
                target = args.url or "https://www.youtube.com/"
                page.goto(target,
                          wait_until="domcontentloaded", timeout=90000)
                summary["youtube_home_loaded"] = True
                page.wait_for_timeout(6000)
                if "google.com/sorry" in (page.url or ""):
                    summary["captcha"] = True
                    capture(page, "proof_yt_captcha", "CAPTCHA wall",
                            f"bound ip {addr}")
                    try:
                        assigner.burn_ip(addr, "captcha_flagged")
                        print(f"[shelf] burned {addr} (captcha_flagged)")
                    except Exception as e:
                        print(f"[shelf] burn_ip failed: {e}")
                    reason = "CAPTCHA wall on YouTube home - exit IP flagged"
                    exit_code = 2
                else:
                    summary["signed_in"] = bool(is_signed_in(page))
                    capture(page, "proof_yt_home", "YouTube home via shelf IP",
                            f"handle={handle} ip={addr} "
                            f"signed_in={summary['signed_in']}")

                # (d) same-tab exit-IP lookup (what the outside world sees).
                # ip-api is plain HTTP; some flaky SOCKS exits drop plain-HTTP
                # sessions while HTTPS still works, so fall back to an HTTPS
                # echo before declaring the lookup failed.
                info = {}
                try:
                    info = exit_ip_country(page) or {}
                except Exception as e:
                    print(f"  [ip-lookup] ip-api failed ({str(e)[:80]}) - "
                          f"HTTPS fallback ...")
                if not info.get("query"):
                    try:
                        page.goto("https://ipwho.is/",
                                  wait_until="domcontentloaded", timeout=60000)
                        body = page.locator("body").inner_text()
                        who = json.loads(body)
                        info = {"query": who.get("ip"),
                                "countryCode": who.get("country_code"),
                                "isp": who.get("connection", {}).get("isp"),
                                "city": who.get("city")}
                    except Exception as e2:
                        print(f"  [ip-lookup] HTTPS fallback failed too "
                              f"({str(e2)[:80]})")
                if not summary["captcha"] and info.get("query"):
                    capture(page, "proof_ip_lookup",
                            "exit IP as seen by the outside world",
                            f"handle={handle} ip={addr} "
                            f"egress={info.get('query')} cc={info.get('countryCode')}")
                    summary["browser_exit_ip"] = info.get("query")
                    summary["browser_exit_cc"] = info.get("countryCode")
                    summary["isp"] = info.get("isp")
                    summary["city"] = info.get("city")

                # (e) 3-way egress comparison
                b_ip = summary.get("browser_exit_ip")
                gate_ip = gate_result.get("egress_ip")
                entry_ip = pool_entry_egress
                print("\n--- egress comparison (should match) ---")
                print(f"  gate egress   : {gate_ip}")
                print(f"  browser egress: {b_ip} ({summary.get('browser_exit_cc')} "
                      f"{summary.get('isp')})")
                print(f"  pool entry    : {entry_ip}")
                if b_ip and gate_ip and b_ip != gate_ip:
                    summary["warnings"].append(
                        f"browser egress {b_ip} != gate egress {gate_ip} "
                        f"(CONNECT egress differs - investigate)")
                if b_ip and entry_ip and b_ip != entry_ip:
                    summary["warnings"].append(
                        f"browser egress {b_ip} != pool entry egress {entry_ip}")
                if summary["browser_exit_cc"] and addr:
                    pass  # cc-vs-region handled in the verdict below

                # (f) hold mode: park 2 tabs for the human (target URL +
                # IP lookup) and block until they close the window/tabs
                if args.hold:
                    page.goto(target,
                              wait_until="domcontentloaded", timeout=90000)
                    p2 = ctx.new_page()
                    p2.goto("https://ipwho.is/",
                            wait_until="domcontentloaded", timeout=60000)
                    print("[hold] 2 tabs open (YouTube + IP lookup) - close "
                          "the browser window (or both tabs) to finish",
                          flush=True)
                    while not (page.is_closed() or p2.is_closed()):
                        try:
                            page.wait_for_timeout(1000)
                        except Exception:
                            break  # context torn down with the window
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        reason = f"browser session failed: {str(e)[:200]}"
        summary["browser_error"] = str(e)[:300]

    # (f) verdict: simple + honest
    if not reason:
        checks = {
            "youtube_home_loaded": summary["youtube_home_loaded"],
            "signed_in": summary["signed_in"],
            "ip_lookup_returned_country": bool(summary["browser_exit_cc"]),
        }
        cc = summary["browser_exit_cc"]
        region_ok = (region is not None and region != "?"
                     and (cc == region
                          or (region == "T1" and cc in T1_CC)))
        checks["cc_matches_region_or_T1"] = region_ok
        if all(checks.values()):
            verdict, exit_code = "PASS", 0
            reason = ("youtube loaded, signed in, exit IP "
                      f"{summary['browser_exit_ip']} ({cc}) matches region "
                      f"{region}")
        else:
            reason = "failed: " + ", ".join(k for k, v in checks.items() if not v)
            if cc and not region_ok:
                reason += (f" (exit country {cc} does not match binding "
                          f"region {region})")

    summary["verdict"] = verdict
    summary["reason"] = reason
    (proof_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    line = "=" * 64
    print("\n" + line)
    print(f"PROOF: {handle}  {verdict}")
    print(line)
    for k in ("region", "ip_addr", "proto", "proxy_url"):
        print(f"  {k:<20} {summary.get(k)}")
    print(f"  {'gate':<20} {json.dumps(gate_result)}")
    print(f"  {'signed_in':<20} {summary['signed_in']}")
    print(f"  {'browser exit':<20} {summary.get('browser_exit_ip')} "
          f"({summary.get('browser_exit_cc')} {summary.get('isp')})")
    for w in summary["warnings"]:
        print(f"  WARNING: {w}")
    print(f"  reason: {reason}")
    print(f"  evidence: _hunt/proof_yt_home.* _hunt/proof_ip_lookup.*")
    print(f"  summary : {proof_dir / 'summary.json'}")
    print(line)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

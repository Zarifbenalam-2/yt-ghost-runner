"""audit_watch_run.py — grade a Cloud Ghost Watcher run from its artifacts.

Downloads every ghost-report-runner-* artifact of one workflow run (run
reports, burn reports, ghost logs) and answers the questions that matter
after a stress test:

  - did the tabs run, and how many reached the watch target?
  - the global per-IP cap: max tabs any ONE address served across ALL
    runners combined (policy cap_per_ip), plus per-runner spread
  - proxy riding: how many tabs actually rode a gold IP vs went direct
  - human moves: fired / planned per runner, straight from the ghost logs
  - what the runners burned (dead-IP feedback that should reach the rack)

Usage (token from GITHUB_TOKEN or --token):
  python audit_watch_run.py --run-id <id> [--cap 5] [--verbose]

Exit code 0 always; the JSON verdict is printed as AUDIT_JSON= for CI.
"""
import argparse
import collections
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile

REPO = os.environ.get("GITHUB_REPOSITORY", "Zarifbenalam-2/yt-ghost-runner")


def _token(cli):
    t = cli or os.environ.get("GITHUB_TOKEN", "")
    if not t:
        print("FATAL: set GITHUB_TOKEN", file=sys.stderr)
        sys.exit(2)
    return t


def api(url, token, timeout=40):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ghost-audit"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def artifact_zip(repo, token, artifact_id, timeout=120):
    """Download an artifact zip, following the signed blob URL WITHOUT auth
    (urllib drops the header on the cross-host redirect and GitHub 401s)."""
    url = f"https://api.github.com/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ghost-audit"})

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        signed = e.headers.get("Location")
    with urllib.request.urlopen(signed, timeout=timeout) as r:
        return r.read()


def human_lines(text):
    """Pull human-move evidence out of a ghost log.

    Two log shapes carry the fired move names:
      "... human move(s) fired at 210s: pause+volume"
      "... watch done: fired human moves ['pause', 'seek']"
    Timestamps contain colons, so the payload is taken after the LAST colon
    (and inside brackets for the summary shape).
    """
    out = collections.Counter()
    fired = collections.Counter()
    for line in text.splitlines():
        low = line.lower()
        if "human move" not in low and "human-move" not in low:
            continue
        out["human_move_lines"] += 1
        out["human_move_text"] = line
        if "fired" not in low:
            continue
        out["human_fired_lines"] += 1
        if "fired at" in low:
            payload = line.rsplit(":", 1)[-1]   # "pause+volume" — the firings
        elif "[" in line and "]" in line:
            # "... watch done: fired human moves ['pause', 'seek']" — the
            # per-watch summary restates the same fires; count it once, not
            # as extra moves
            out["human_done_lines"] += 1
            continue
        else:
            continue
        for mv in re.split(r"[^A-Za-z]+", payload):
            if mv and mv.lower() not in ("at", "s"):
                fired[mv.lower()] += 1
    for k, v in fired.items():
        out[f"fired_{k}"] = v
    out["fired_total"] = sum(fired.values())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--token", default="")
    ap.add_argument("--cap", type=int, default=5)
    args = ap.parse_args()
    token = _token(args.token)

    arts = api(f"https://api.github.com/repos/{args.repo}/actions/runs/"
               f"{args.run_id}/artifacts?per_page=100", token)
    reports = [a for a in arts.get("artifacts", []) if a["name"].startswith("ghost-report-runner-")]
    print(f"run {args.run_id}: {len(reports)} runner report artifacts")

    tabs_ok = tabs_req = 0
    per_runner = {}
    ip_tabs = collections.Counter()      # addr -> tabs across ALL runners
    direct_tabs = 0
    parked_tabs = 0
    deadline_tabs = 0
    burned = set()
    moves = collections.Counter()
    events = {}          # ip -> [(t, +1/-1)] for concurrency
    resources = {}
    for a in reports:
        rid = a["name"].rsplit("-", 1)[-1]
        try:
            z = zipfile.ZipFile(io.BytesIO(artifact_zip(args.repo, token, a["id"])))
        except Exception as exc:
            print(f"  runner {rid}: artifact download failed: {exc}")
            continue
        r = {}
        logtxt = ""
        for name in z.namelist():
            try:
                body = z.read(name).decode("utf-8")
            except Exception:
                continue
            if "run_report" in name:
                try:
                    r = json.loads(body)
                except json.JSONDecodeError:
                    pass
            elif name.startswith("ghost_log") and name.endswith(".txt"):
                logtxt = body
        if not r:
            print(f"  runner {rid}: no run_report.json inside")
            continue
        ok, req_n = r.get("tabs_ok", 0), r.get("tabs_requested", 0)
        tabs_ok += ok
        tabs_req += req_n
        usage = r.get("ip_usage", {}) or {}
        ip_tabs.update({k: v for k, v in usage.items() if isinstance(v, int)})
        # concurrency per IP (a swept count of overlapping tab intervals),
        # because with tab-level IP reuse a lifetime total says nothing about
        # how many tabs rode one exit at once
        for x in r.get("results", []):
            ipd = x.get("ip")
            if not ipd or ipd in ("direct", "parked", "cut"):
                continue
            a, b = x.get("started_at"), x.get("ended_at")
            if a and b:
                events.setdefault(ipd, []).append((a, 1))
                events.setdefault(ipd, []).append((b, -1))
        direct_tabs += sum(1 for x in r.get("results", []) if x.get("ip") == "direct")
        # a parked tab was never sent anywhere: it waited for a live exit and
        # gave up (no_live_exit). Counting it as "on proxies" would overstate
        # the pool's reach exactly when the pool was empty.
        parked_tabs += sum(1 for x in r.get("results", []) if x.get("ip") == "parked")
        deadline_tabs += sum(1 for x in r.get("results", [])
                             if x.get("error") == "run_deadline")
        burned.update(r.get("burned_ips", []) or [])
        hl = human_lines(logtxt)
        resources[rid] = r.get("resources", {})
        per_runner[rid] = {
            "ok": ok, "req": req_n,
            "pool": r.get("pool", {}),
            "ip_usage": usage,
            "dead_in_slice": len(r.get("dead_in_slice", []) or []),
            "human_moves": dict(hl),
        }
        print(f"  runner {rid}: {ok}/{req_n} ok, {len(usage)} IPs used, "
              f"{len(r.get('dead_in_slice', []) or [])} slice-dead, moves={dict(hl)}")

    worst_ip, worst_n = ("", 0)
    if ip_tabs:
        worst_ip, worst_n = ip_tabs.most_common(1)[0]
    # max CONCURRENT tabs on one address (the number the cap actually governs)
    peak_ip, peak_n = ("", 0)
    for ipd, evs in events.items():
        cur = peak = 0
        for _t, d in sorted(evs, key=lambda e: (e[0], e[1])):
            cur += d
            peak = max(peak, cur)
        if peak > peak_n:
            peak_ip, peak_n = ipd, peak
    verdict = {
        "run_id": int(args.run_id),
        "runners_reporting": len(per_runner),
        "tabs_requested": tabs_req,
        "tabs_ok": tabs_ok,
        "tab_success_rate": round(tabs_ok / tabs_req, 3) if tabs_req else 0,
        "tabs_on_proxies": (tabs_req - direct_tabs - parked_tabs) if tabs_req else 0,
        "tabs_direct": direct_tabs,
        "tabs_parked": parked_tabs,
        "tabs_cut_at_deadline": deadline_tabs,
        "distinct_ips_ridden": len(ip_tabs),
        "max_tabs_one_ip": worst_n,
        "max_tabs_one_ip_addr": worst_ip,
        "max_concurrent_tabs_one_ip": peak_n,
        "max_concurrent_tabs_one_ip_addr": peak_ip,
        "cap": args.cap,
        "cap_respected": (peak_n or worst_n) <= args.cap,
        "burned_by_runners": len(burned),
    }
    print("\n=== VERDICT ===")
    for k, v in verdict.items():
        print(f"  {k}: {v}")
    print(f"  resources: " + json.dumps({k: {kk: resources[k].get(kk) for kk in
          ("cpu_count", "loadavg_1m", "mem_avail_mb") if kk in resources.get(k, {})}
          for k in list(resources)[:3]}))
    print("AUDIT_JSON=" + json.dumps(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

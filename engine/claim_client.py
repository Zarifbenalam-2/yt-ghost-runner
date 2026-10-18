"""claim_client.py — talk to the hub's central claim endpoint.

Each tab (seus own Chromium process + proxy) normally draws an exit from the
runner-slice; when CLAIM_URL (the relay on your PC) is set, it claims ATOMICALLY
from the hub instead, giving a true GLOBAL one-tab-per-IP cap across the whole
fleet without anyone holding state. Read-only fallback: every failure just
returns None and draw_gold_ip continues slicing locally, so the central claim
never becomes a new point of failure.
"""
import json
import os
import threading
import urllib.request


def owner_tag():
    """Stable identity for claims on this runner: RUNNER_ID:process_id.

    The RUNNER_ID distinguishes fleet runners (each is a separate job). The
    process id separates tabs within a runner is NOT enough (all 28 tabs share
    the runner's process), so callers pass their tab index too and combine.
    Here we return the runner part; the tab index is appended by draw_gold_ip.
    """
    rid = os.environ.get("RUNNER_ID", os.environ.get("MATRIX_RUNNER_ID", "local"))
    pid = os.getpid()
    return f"{rid}:{pid}"


def _base(claim_url):
    return claim_url.rstrip("/")


def _post(url, payload, timeout=4):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    tok = os.environ.get("RELAY_TOKEN", "")
    if tok:
        headers["Authorization"] = f"Token {tok}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def try_claim(claim_url, owner, cap_per_ip=1):
    """Ask the hub to lease one pooled exit to `owner`. None on failure."""
    try:
        d = _post(_base(claim_url) + "/api/claim",
                  {"owner": owner, "cap_per_ip": cap_per_ip})
        if d.get("ok"):
            return d.get("exit")
    except Exception:
        pass
    return None


def release(claim_url, addr, owner):
    try:
        _post(_base(claim_url) + "/api/release", {"addr": addr, "owner": owner})
    except Exception:
        pass

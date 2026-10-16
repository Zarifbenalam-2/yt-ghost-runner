"""config.py — THE single source of truth for every tunable.

Before this module, the T1 country list existed in 5 files, the repo name
in 3, and timeout/cap values disagreed across 6 places. Everything that
used to be a scattered constant or a magic number lives here, and can be
overridden by environment variable (so CI and the dashboard can retune
without editing code).

Precedence: explicit env var > default below.
"""
import os
from pathlib import Path

# ----------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
IPSHELF_DATA = Path(os.environ.get("IPSHELF_DATA") or (ROOT / "ipshelf" / "data"))
SNAPSHOT_FILE = DATA_DIR / "ip_pool_snapshot.json"
POLICY_FILE = DATA_DIR / "policy.json"

# ----------------------------------------------------------------- identity
#: owner/repo for GitHub API calls. Env wins so a fork/second account works.
REPO = os.environ.get("GITHUB_REPOSITORY", "Zarifbenalam-2/yt-ghost-runner")
WORKFLOW_WATCH = "cloud_ghost_watch.yml"
WORKFLOW_FACTORY = "gold_factory.yml"

# ----------------------------------------------------------------- network
def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _int("DASHBOARD_PORT", 8766)
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")   # empty = loopback-only trust
RELAY_PORT = _int("RELAY_PORT", 8899)

#: GitHub API: timeout, retries, base backoff (seconds). Used by hub.gh.
GH_TIMEOUT = _int("GH_TIMEOUT", 20)
GH_RETRIES = _int("GH_RETRIES", 4)
GH_BACKOFF = float(os.environ.get("GH_BACKOFF", "1.5"))

#: proxy gold-check per-attempt cap. 35s proven necessary: the YouTube watch
#: page is ~1.3MB and slow-but-alive proxies need >15s even gzipped.
GATE_TIMEOUT = _int("GATE_TIMEOUT", 35)
WATCH_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# ----------------------------------------------------------------- scale / policy
#: Law 2 — max channels (tabs) served by one exit IP.
CAP_PER_IP = _int("CAP_PER_IP", 5)
#: Proven ceiling: 24 tabs/runner passed 96/96; 30 collapsed to goto-timeouts.
TABS_PER_RUNNER = _int("TABS_PER_RUNNER", 24)
#: Free-plan concurrency per account.
RUNNERS_PER_ACCOUNT = _int("RUNNERS_PER_ACCOUNT", 20)
#: Per-account job split: tab runners vs harvest runners (18 : 2 default).
TAB_RUNNERS = _int("TAB_RUNNERS", 18)
HARVEST_RUNNERS = _int("HARVEST_RUNNERS", 2)

#: gold_factory cycle knobs.
FACTORY_CYCLE_SEC = _int("FACTORY_CYCLE_SEC", 300)
FACTORY_PAR = _int("FACTORY_PAR", 500)
FACTORY_MAX_TEST = _int("FACTORY_MAX_TEST", 8000)
FACTORY_SEEN_TTL_DAYS = _int("FACTORY_SEEN_TTL_DAYS", 7)

#: how often the shelf is re-gated (minutes) — policy-driven, dashboard-visible.
SHELF_REGATE_MINUTES = _int("SHELF_REGATE_MINUTES", 5)

# ----------------------------------------------------------------- regions
#: Law 3 — tier-1 commons. Defined ONCE; import this everywhere.
T1_SET = frozenset({
    "US", "GB", "CA", "DE", "FR", "NL", "SE", "CH", "AT", "DK", "NO",
    "FI", "IE", "BE", "LU", "JP", "SG", "AU", "NZ", "KR", "HK", "TW",
})
T2_SET = frozenset({
    "ES", "IT", "PT", "PL", "CZ", "SK", "HU", "RO", "BG", "GR", "HR", "SI",
    "EE", "LV", "LT", "IL", "AE", "QA", "SA", "TR", "MY", "TH", "VN", "IN",
    "ID", "PH", "BR", "MX", "AR", "CL", "CO", "ZA", "RU", "UA",
})


def tier_of(cc):
    """Country code -> 'T1' | 'T2' | 'T3'."""
    cc = (cc or "").upper()
    if cc in T1_SET:
        return "T1"
    if cc in T2_SET:
        return "T2"
    return "T3"


def is_t1(cc):
    return (cc or "").upper() in T1_SET
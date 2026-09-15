"""shelf.py — pool + bindings store for the IP Shelf.

THE LAWS this module enforces (user decisions, locked 2026-09-10):

  LAW 1  One channel = one sticky gold IP, for life.
         A binding records channel -> region + sticky_ip; the IP changes
         ONLY when it dies or fails a gold re-check (burn-and-replace).
  LAW 2  Max 5 channels per IP (CAP). A deliberately small footprint so
         Google sees nothing unusual behind one exit address.
  LAW 3  Region lock is forever. Once a channel has picked a region
         (country code like "US", or the "T1" commons) it never draws
         from another shelf. The binding keeps region even when the
         sticky IP is released.
  LAW 4  Only graded gold counts. An exit enters the pool only after the
         hunt pipeline proved it fetched the real YouTube watch page and
         got "playabilityStatus": "OK". Everything else is not on a shelf.
  LAW 5  Burn and delete on failure. A dead / non-gold exit is removed
         from the pool entirely, its channels are released, and the next
         draw happens on the same shelf. We do not keep corpses.

Storage: two JSON files in the data dir (env var IPSHELF_DATA overrides
the default <ipshelf>/data — used by tests):
  shelf_pool.json  {"updated": ts, "exits": [entry, ...]}
  bindings.json   {"@Handle": {region, sticky_ip, bound_since, history}}

All writes are atomic (write .tmp then os.replace) so a crash or a
concurrent reader never sees a half-written file.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- constants

#: LAW 2 — max channels served by a single gold IP simultaneously.
CAP = 5

#: LAW 3 — the tier-1 commons (mirrors hunt machine exitpool.py T1).
#: "T1" is a valid pseudo-region: a channel locked to "T1" draws from
#: any tier-1 country's shelf.
T1_SET = {"US", "GB", "CA", "DE", "FR", "NL", "SE", "CH", "AT", "DK", "NO",
          "FI", "IE", "BE", "LU", "JP", "SG", "AU", "NZ", "KR", "HK", "TW"}

# ------------------------------------------------------------------- paths

_PKG_DIR = Path(__file__).resolve().parent.parent        # .../ipshelf


def data_dir() -> Path:
    """Data directory: env IPSHELF_DATA wins, else <ipshelf>/data."""
    env = os.environ.get("IPSHELF_DATA")
    return Path(env) if env else _PKG_DIR / "data"


def pool_path() -> Path:
    return data_dir() / "shelf_pool.json"


def bindings_path() -> Path:
    return data_dir() / "bindings.json"


def log_path() -> Path:
    return data_dir() / "ipshelf.log"


def sweeps_dir() -> Path:
    return data_dir() / "sweeps"


# ------------------------------------------------------------- time helper

def now_ts() -> str:
    """UTC timestamp like '2026-09-10T12:00:00Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ atomic writes

# One process-wide store lock: the server handles requests in threads and
# read-modify-write sequences (draw/bind/burn) must not interleave on the
# pool/bindings files. Atomic .tmp+replace alone only protects a single
# write; this lock protects the whole RMW cycle at 100+-channel scale.
_STORE_LOCK = threading.RLock()
_UNIQUE = str(os.getpid()) + "-" + str(id(_STORE_LOCK))


def _atomic_write_json(path: Path, obj) -> None:
    """Write JSON atomically: unique .tmp file, then os.replace.

    On Windows, os.replace can fail with WinError 5 (Access denied) when
    another thread in this process has the destination open for reading
    at that instant (load_bindings vs save_bindings racing). A short retry
    closes that gap; after MAX tries the error is a real one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + _UNIQUE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    _replace_with_retry(tmp, path)


def _replace_with_retry(tmp: Path, path: Path, tries: int = 50,
                        delay: float = 0.02) -> None:
    """os.replace with a Windows-sharing-violation retry."""
    for attempt in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay)


def store_lock():
    """The store-wide RLock — wrap any multi-step read-modify-write."""
    return _STORE_LOCK


def append_log(evt: str, **fields) -> None:
    """Append one JSON line to ipshelf.log (best-effort, never raises)."""
    try:
        line = {"ts": now_ts(), "evt": evt}
        line.update(fields)
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging must never take down an operation


# ---------------------------------------------------------------- pool CRUD

def load_pool() -> dict:
    """Load shelf_pool.json; missing/corrupt file -> empty pool."""
    p = pool_path()
    if not p.is_file():
        return {"updated": now_ts(), "exits": []}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"updated": now_ts(), "exits": []}


def save_pool(pool: dict) -> None:
    """Atomic save of the pool (sets 'updated' to now)."""
    pool["updated"] = now_ts()
    _atomic_write_json(pool_path(), pool)


def load_bindings() -> dict:
    """Load bindings.json; missing/corrupt file -> empty bindings."""
    p = bindings_path()
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_bindings(bindings: dict) -> None:
    """Atomic save of the bindings map."""
    _atomic_write_json(bindings_path(), bindings)


def get_exit(pool: dict, addr: str):
    """Return the exit entry with this addr, or None."""
    for e in pool.get("exits", []):
        if e.get("addr") == addr:
            return e
    return None


# ------------------------------------------------------------- shelf queries

def free_ips(pool: dict, region: str) -> list:
    """Free (bindable) gold exits for a region, best score first.

    'region' is either a country code ("US") or the pseudo-region "T1"
    (every tier-1 country's shelf combined). An exit is free when its
    status is "gold" and it still has channel slots left (LAW 2, CAP).
    """
    out = []
    for e in pool.get("exits", []):
        if e.get("status") != "gold":
            continue
        if len(e.get("channels", [])) >= CAP:
            continue
        if region == "T1":
            if e.get("tier") != "T1":
                continue
        else:
            if e.get("cc") != region:
                continue
        out.append(e)
    out.sort(key=lambda e: -e.get("score", 0))
    return out


def shelf_regions(pool: dict) -> list:
    """One summary row per country present, plus the "T1" commons row.

    Row shape (slot arithmetic is self-consistent):
      {"region": cc, "count": exits on shelf, "capacity": count*CAP slots,
       "assigned": bound channel slots, "free": capacity - assigned,
       "needs_attention": free < 2}

    "free" is FREE CHANNEL SLOTS (how many more channels the shelf can
    still take), matching needs_harvest semantics. The "T1" row is an
    aggregate over every tier-1 exit (same exits also appear on their
    own cc row — it is a commons view, not extra inventory).

    Sorted: the "T1" row first, then by count descending.
    """
    rows = {}
    t1 = {"region": "T1", "count": 0, "capacity": 0, "assigned": 0}
    for e in pool.get("exits", []):
        cc = e.get("cc") or "??"
        r = rows.setdefault(cc, {"region": cc, "count": 0,
                                 "capacity": 0, "assigned": 0})
        ch = len(e.get("channels", []))
        r["count"] += 1
        r["capacity"] += CAP
        r["assigned"] += ch
        if e.get("tier") == "T1":
            t1["count"] += 1
            t1["capacity"] += CAP
            t1["assigned"] += ch

    out = []
    for r in rows.values():
        row = dict(r)
        row["free"] = row["capacity"] - row["assigned"]
        row["needs_attention"] = bool(row["free"] < 2)
        out.append(row)
    t1["free"] = t1["capacity"] - t1["assigned"]
    t1["needs_attention"] = bool(t1["free"] < 2)
    out.append(t1)
    out.sort(key=lambda r: (0 if r["region"] == "T1" else 1, -r["count"]))
    return out


# ------------------------------------------------------------------- merging

def merge_exits(pool: dict, new_exits: list) -> dict:
    """Merge freshly graded exits into the pool, keyed by addr.

    Existing entry: keeps channels/first_seen (the sticky-binding side),
    refreshes every grade field and stamps last_gold_ok=now, bumps
    ok_cycles. New addr: appended with channels=[] and status "gold".

    Returns {"added": n, "refreshed": n}.
    """
    now = now_ts()
    index = {e.get("addr"): (i, e) for i, e in enumerate(pool.get("exits", []))}
    added = refreshed = 0
    for ne in new_exits:
        addr = ne.get("addr")
        if not addr:
            continue
        if addr in index:
            i, old = index[addr]
            merged = dict(ne)
            # sticky-side fields survive a re-grade (LAW 1):
            merged["channels"] = old.get("channels", [])
            merged["first_seen"] = old.get("first_seen") or ne.get("first_seen") or now
            merged["ok_cycles"] = old.get("ok_cycles", 0) + 1
            merged["last_gold_ok"] = now
            merged["status"] = "gold"
            pool["exits"][i] = merged
            refreshed += 1
        else:
            entry = dict(ne)
            entry["channels"] = list(ne.get("channels", []))
            entry["first_seen"] = ne.get("first_seen") or now
            entry["ok_cycles"] = ne.get("ok_cycles", 1)
            entry["last_gold_ok"] = now
            entry["status"] = "gold"
            if "channels" not in ne:
                entry["channels"] = []
            pool.setdefault("exits", []).append(entry)
            index[addr] = (len(pool["exits"]) - 1, entry)
            added += 1
    pool["updated"] = now
    return {"added": added, "refreshed": refreshed}

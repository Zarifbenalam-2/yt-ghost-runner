"""allocator.py — global per-IP cap enforcement across runners.

THE PROBLEM (proven on watch run #43):
  4 runners each independently sorted the snapshot by score and picked the
  same top IPs. Result: one IP served 13+ tabs while policy says 5. Each
  runner had its own throwaway copy of the rack, so no runner could know
  what the others chose.

THE FIX (no server needed, works with a stateless snapshot):
  Pre-partition the snapshot into per-runner SLICES at collect time.
  Each runner gets its own file listing ONLY the IPs it may use, with each
  IP appearing in at most `cap_per_ip` runners' slices at once. The runner
  loads its slice, and even if it puts every tab on one IP it stays within
  the global budget... unless it exceeds cap within its own slice, which
  the runner-side counter bounds too.

  Shared IPs are capped: an address is assigned to at most CAP runners
  (round-robin), so worst case tabs on one IP = CAP x tabs_per_runner,
  which is why the runner-side per-IP counter also clamps to CAP.

Files written: data/snapshot_shard_<i>_of_<n>.json  (one per runner slot)
"""
import json
import math
from pathlib import Path

from . import config, db
from .logging_setup import get_logger

log = get_logger("hub.allocator")


def build_sharded_snapshot(n_runners, path_prefix=None, cap=None):
    """Commit-time slices — delegates to build_slices_from_file (one rule).

    This used to place each IP round-robin into up to `cap` slices, while the
    runners sliced by hash ownership (`owner_of`). Two schemes, two answers:
    an IP could sit in `cap` slices here AND pass a different runner's refresh
    filter, so one exit collected cap x cap tabs (watch #78: 12 on one IP with
    cap 4, seen as 4+4+4). The committed `data/snapshot_shard_*` files are
    meant to be exactly what a runner would rebuild, so they now come from the
    same code path — reach=1, hash ownership, one IP in one slice.

    The snapshot file must already be written (both call sites run this right
    after broker.write_snapshot()); nothing is read from the registry.
    """
    prefix = Path(path_prefix) if path_prefix else (config.DATA_DIR / "snapshot_shard")
    res = build_slices_from_file(n_runners, snapshot_path=config.SNAPSHOT_FILE,
                                out_prefix=prefix, cap=cap, reach=1)
    return {"runners": res["runners"], "slices": res["slices"],
            "unique_ips": res["exits"], "capped_ips": 0}


def load_my_slice(runner_index, n_runners, base=None):
    """Runner side: load this runner's slice, else fall back to full snapshot."""
    base = base or (config.DATA_DIR / "snapshot_shard")
    p = Path(f"{base}_{runner_index}_of_{n_runners}.json")
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("exits", [])
        except (OSError, json.JSONDecodeError):
            pass
    # fallback: whole snapshot
    try:
        return json.loads(Path(config.SNAPSHOT_FILE).read_text(
            encoding="utf-8")).get("exits", [])
    except (OSError, json.JSONDecodeError):
        return []


# --------------------------------------------------------------------------
# Deterministic file-based slicing — built ON the runner, for the runner
# count the workflow actually uses.
#
# WHY THIS EXISTS: build_sharded_snapshot() above runs at collect time and
# needs the registry, so the collector had to guess the fleet size
# (SLICE_RUNNERS=4). A 20-runner watch then found no `_of_20` file, fell
# back to the WHOLE snapshot, and every runner picked the same top-scored
# addresses — the exact run-#43 cap violation coming back. Slicing from the
# committed snapshot file on each runner makes the partition self-consistent
# for any runner count, with no registry and no guess.
#
# `reach` = how many runners may share one address. reach=1 means an address
# belongs to exactly one runner, so `cap_per_ip` tabs on it is a REAL global
# cap (worst case = cap, not cap x tabs_per_runner).
# --------------------------------------------------------------------------

def _policy_cap(default):
    """cap_per_ip from the registry — or the default on a stateless runner.

    The runner-side CLI runs BEFORE any hub.db exists (fresh checkout: the
    sqlite file either isn't there or has no tables yet), so this must never
    raise — watch #49 died in exactly that spot on 19 runners at once.
    """
    try:
        return int(db.get_policy("cap_per_ip", default))
    except Exception:
        return int(default)


def owner_of(addr, n_runners):
    """THE canonical owner of an address: runner index in [0, n_runners).

    One ownership rule for the whole system, stable across files and runs
    (hash of the address, never its position in some file). Two schemes
    previously coexisted — round-robin position in the slice builder, md5 in
    the watcher's mid-run refresh — and they disagreed: an IP in runner 12's
    slice could also pass runner 15's refresh filter, so one exit entered
    three pools and collected 3 x cap tabs (watch #78: 12 tabs on one IP with
    cap 4, seen as 4+4+4). One rule, or the per-IP cap is decorative.
    """
    import hashlib
    n = max(1, int(n_runners))
    h = int(hashlib.md5(addr.encode("utf-8")).hexdigest()[:8], 16)
    return h % n


def build_slices_from_file(n_runners, snapshot_path=None, out_prefix=None,
                           cap=None, reach=1):
    """Slice a snapshot FILE into n_runners per-runner files.

    Ownership is hash-based (owner_of) so every exit lands in exactly ONE
    slice, agreeing with every other component that asks whose IP this is.
    reach>1 is still honoured for callers that deliberately want an address
    visible to more runners (that multiplies the effective per-IP cap).

    Deterministic: same pool + same n_runners -> same slices on every runner.
    Returns {"runners", "exits", "slices": [...], "reach"}.
    """
    snapshot_path = Path(snapshot_path or config.SNAPSHOT_FILE)
    out_prefix = Path(out_prefix or (config.DATA_DIR / "snapshot_shard"))
    cap = int(cap if cap is not None else _policy_cap(config.CAP_PER_IP))
    reach = max(1, int(reach))
    n_runners = max(1, int(n_runners))

    try:
        exits = json.loads(snapshot_path.read_text(encoding="utf-8")).get("exits", [])
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("allocator: snapshot unreadable (%s)", exc)
        exits = []

    slices = [[] for _ in range(n_runners)]
    for e in exits:
        if not e.get("addr"):
            continue
        start = owner_of(e["addr"], n_runners)
        for k in range(reach):
            slices[(start + k) % n_runners].append(e)

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    counts = []
    for idx, sl in enumerate(slices):
        p = Path(f"{out_prefix}_{idx}_of_{n_runners}.json")
        p.write_text(json.dumps({
            "runner_index": idx, "of_runners": n_runners,
            "cap_per_ip": cap, "reach": reach, "exits": sl,
        }, indent=2), encoding="utf-8")
        counts.append(len(sl))

    res = {"runners": n_runners, "exits": len(exits), "slices": counts,
           "reach": reach, "cap_per_ip": cap}
    log.info("allocator: %d exits -> %d slices (reach=%d, cap=%d): %s",
             len(exits), n_runners, reach, cap, counts)
    return res


def _cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Slice a gold snapshot for the fleet")
    ap.add_argument("--slices", type=int, required=True, help="number of runner shards")
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--out-prefix", default=None)
    ap.add_argument("--cap", type=int, default=None, help="max tabs per IP")
    ap.add_argument("--reach", type=int, default=1,
                    help="how many runners may share one IP (1 = true global cap)")
    a = ap.parse_args(argv)
    res = build_slices_from_file(a.slices, a.snapshot, a.out_prefix, a.cap, a.reach)
    print("SLICE_SUMMARY=" + json.dumps(res), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
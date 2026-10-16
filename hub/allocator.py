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
    """Partition gold gold into n_runners slices, capping each IP's reach.

    Returns {"runners": n, "slices": [...counts...], "unique_ips": n, "capped_ips": n}
    """
    cap = cap or db.get_policy("cap_per_ip", config.CAP_PER_IP)
    gold = db.fetch_gold(limit=100000)
    if not gold:
        return {"runners": n_runners, "slices": [], "unique_ips": 0, "capped_ips": 0}

    # round-robin assignment: each IP lands in at most `cap` slices
    slices = [[] for _ in range(n_runners)]
    capped = 0
    for i, ip in enumerate(gold):
        start = i % n_runners
        placed = 0
        for k in range(n_runners):
            if placed >= cap:
                capped += 1
                break
            slices[(start + k) % n_runners].append(ip)
            placed += 1

    prefix = Path(path_prefix) if path_prefix else (config.DATA_DIR / "snapshot_shard")
    written = []
    for idx, sl in enumerate(slices):
        p = Path(f"{prefix}_{idx}_of_{n_runners}.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"snapshotted_at": db.now_iso() if hasattr(db, "now_iso") else "",
                   "runner_index": idx, "of_runners": n_runners,
                   "cap_per_ip": cap, "exits": [
                       {k: e[k] for k in ("addr", "proto", "cc", "tier", "kind",
                                          "score", "egress_ip", "latency_ms")
                        if k in e} for e in sl]}
        if hasattr(db, "now_iso"):
            payload["snapshotted_at"] = db.now_iso()
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        written.append(len(sl))

    res = {"runners": n_runners, "slices": written, "unique_ips": len(gold),
           "capped_ips": capped}
    log.info("allocator: %d IPs -> %d slices (each IP in <=%d slices): %s",
             len(gold), n_runners, cap, written)
    return res


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
"""assigner.py — the draw. Resolves one channel to one sticky gold IP.

THE LAWS this module enforces:

  LAW 1  One channel = one sticky gold IP, for life. If the channel
         already holds a live gold IP, it keeps it (re-gated).
  LAW 2  Max 5 channels per IP — the draw only ever picks exits with
         a free slot (shelf.free_ips).
  LAW 3  Region lock is forever. region = explicit argument, else the
         channel's saved binding region. A channel with no region at
         all returns {"error": "no_region"} — the caller must ask the
         user (region choice is a human decision).
  LAW 4  Only graded gold counts + LAW 6 quick gold gate before bind:
         with a gate_fn supplied, every candidate is re-gated and the
         first PASS binds; failures are skipped (logged).
  LAW 5  Burn and delete on failure: a sticky IP that fails its re-gate
         is deleted from the pool and every channel bound to it is
         released before the channel draws again.
  LAW 8  Next-day reuse: sticky pass -> same IP returned fresh=False;
         sticky fail -> burn + fresh draw from the same shelf.

Every operation appends a JSON line to ipshelf/data/ipshelf.log
(evt: bind / release / burn / assign_region / skip / draw_error).
"""

from . import gate as gate_mod
from . import shelf
from .shelf import now_ts


def _normalize_key(channel_key: str) -> str:
    """Normalize a channel handle: strip whitespace."""
    return (channel_key or "").strip()


def draw_for_channel(channel_key, region=None, gate_fn=None,
                     pool=None, bindings=None) -> dict:
    """Draw a sticky gold IP for one channel.

    Args:
      channel_key  YouTube handle, e.g. "@ZarifTestChannel01" (stripped).
      region       explicit region ("US" country code or "T1"); wins over
                   the saved binding region (used when locking a region
                   at first task).
      gate_fn      optional quick-gate callable gate_fn(addr, proto) -> {"ok": bool}.
                   None = trust stored gold status (offline / dry-run mode).
      pool / bindings  pre-loaded state (else loaded from disk).

    Returns one of:
      {"addr", "proto", "proxy_url", "proxy_variants", "entry", "fresh": bool}
      {"error": "no_region"}                                   — ask the user
      {"error": "shelf_empty", "region": ...}                  — harvest time
    """
    key = _normalize_key(channel_key)
    # whole draw = one read-modify-write cycle on the shared store: hold the
    # store lock so concurrent draws (server threads / worker / watch runs)
    # can never interleave and lose each other's binds.
    with shelf.store_lock():
        if pool is None:
            pool = shelf.load_pool()
        if bindings is None:
            bindings = shelf.load_bindings()

        binding = bindings.get(key, {})

        # ---- 0) region lock (LAW 3): if the binding already has a region,
        # an explicit *different* region is refused. An explicit matching
        # region (or a first-time region) proceeds normally.
        locked = binding.get("region")
        if region and locked and region != locked:
            shelf.append_log("draw_error", channel=key,
                             error="region_locked", current=locked,
                             requested=region)
            return {"error": "region_locked", "current": locked,
                    "requested": region}

        # ---- 1) sticky reuse: does this channel already hold a live gold IP?
        sticky_addr = binding.get("sticky_ip")
        if sticky_addr:
            entry = shelf.get_exit(pool, sticky_addr)
            if (entry and entry.get("status") == "gold"
                    and key in entry.get("channels", [])):
                if gate_fn is None:
                    return _result(entry, fresh=False)
                g = _safe_gate(gate_fn, sticky_addr, entry.get("proto"))
                if g.get("ok"):
                    return _result(entry, fresh=False)
                # gate failed -> burn and fall through to a fresh draw
                burn_ip(sticky_addr, "gate_fail", pool=pool,
                        bindings=bindings)
            else:
                # stale binding (IP gone / not gold / not bound) -> clean it
                _clear_sticky(binding, key, "stale_ip", bindings=bindings)
                bindings[key] = binding

        # ---- 2) which shelf does this channel draw from?
        region = region or locked
        if not region:
            shelf.append_log("draw_error", channel=key, error="no_region")
            return {"error": "no_region"}

        # ---- 3) fresh draw, best score first, gate each candidate
        candidates = shelf.free_ips(pool, region)
        for cand in candidates:
            if key in cand.get("channels", []):
                continue  # already listed (shouldn't happen) — safety
            if gate_fn is not None:
                g = _safe_gate(gate_fn, cand["addr"], cand.get("proto"))
                if not g.get("ok"):
                    shelf.append_log("skip", channel=key, addr=cand["addr"],
                                     reason="gate_fail")
                    continue
            return _bind(key, region, cand, binding, pool, bindings)

        shelf.append_log("draw_error", channel=key, error="shelf_empty",
                         region=region)
        return {"error": "shelf_empty", "region": region}


def _safe_gate(gate_fn, addr, proto):
    """Call gate_fn, never raising (a crashed gate counts as a fail)."""
    try:
        return gate_fn(addr, proto) or {}
    except Exception:
        return {"ok": False}


def _result(entry, fresh: bool) -> dict:
    """Full draw result dict for a (sticky or fresh) entry."""
    return {
        "addr": entry["addr"],
        "proto": entry.get("proto", "HTTP"),
        "proxy_url": gate_mod.proxy_url(entry),
        "proxy_variants": gate_mod.proxy_url_variants(entry),
        "entry": entry,
        "fresh": fresh,
    }


def _bind(key, region, entry, binding, pool, bindings) -> dict:
    """Bind channel -> entry (LAW 1 + LAW 2) and persist everything."""
    now = now_ts()
    reason = "initial" if not binding.get("history") else "replace"
    channels = entry.setdefault("channels", [])
    if key not in channels:
        channels.append(key)

    hist = list(binding.get("history", []))
    # close any dangling previous occupancy
    for h in hist:
        if h.get("to") is None:
            h["to"] = now
            h.setdefault("reason", "replaced")
    hist.append({"ip": entry["addr"], "from": now, "to": None,
                 "reason": reason})

    binding["region"] = region
    binding["sticky_ip"] = entry["addr"]
    binding["bound_since"] = now
    binding["history"] = hist
    bindings[key] = binding

    shelf.save_pool(pool)
    shelf.save_bindings(bindings)
    shelf.append_log("bind", channel=key, addr=entry["addr"], region=region,
                     reason=reason)
    return _result(entry, fresh=True)


def _clear_sticky(binding, key, reason, bindings=None):
    """Close the current sticky occupancy in history (keep region!).

    A dangling history item (to=None) gets to=now and its reason is
    overwritten with the close reason ("why this occupancy ended").
    """
    now = now_ts()
    hist = list(binding.get("history", []))
    for h in hist:
        if h.get("to") is None:
            h["to"] = now
            h["reason"] = reason
    binding["history"] = hist
    binding["sticky_ip"] = None
    binding["bound_since"] = None
    # region is intentionally NOT cleared (LAW 3: locked forever)


def release(channel_key, reason="manual", pool=None, bindings=None) -> dict:
    """Release a channel from its sticky IP (region survives — LAW 3).

    Removes the channel from the exit's channels list, closes the last
    history entry with the reason, clears sticky_ip. Returns a summary.
    """
    key = _normalize_key(channel_key)
    with shelf.store_lock():
        if pool is None:
            pool = shelf.load_pool()
        if bindings is None:
            bindings = shelf.load_bindings()

        binding = bindings.get(key)
        if not binding:
            return {"channel": key, "released": False, "reason": "no_binding"}

        addr = binding.get("sticky_ip")
        _clear_sticky(binding, key, reason)
        bindings[key] = binding

        if addr:
            entry = shelf.get_exit(pool, addr)
            if entry:
                entry["channels"] = [c for c in entry.get("channels", [])
                                     if c != key]

        shelf.save_pool(pool)
        shelf.save_bindings(bindings)
        shelf.append_log("release", channel=key, addr=addr, reason=reason)
        return {"channel": key, "released": True, "addr": addr,
                "region": binding.get("region"), "reason": reason}


def burn_ip(addr, reason, pool=None, bindings=None) -> dict:
    """LAW 5 — burn and delete: remove an exit entirely and free its channels.

    Deletes the exit from the pool, releases every channel bound to it
    (each gets a history entry closed with the reason). Regions survive
    on the bindings (LAW 3) so channels re-draw from the same shelf.
    Returns {"released": [handles]}.
    """
    with shelf.store_lock():
        if pool is None:
            pool = shelf.load_pool()
        if bindings is None:
            bindings = shelf.load_bindings()

        entry = shelf.get_exit(pool, addr)
        channels = list(entry.get("channels", [])) if entry else []
        exits = [e for e in pool.get("exits", []) if e.get("addr") != addr]
        pool["exits"] = exits

        now = now_ts()
        for key in channels:
            binding = bindings.get(key)
            if not binding:
                continue
            if binding.get("sticky_ip") == addr:
                hist = list(binding.get("history", []))
                for h in hist:
                    if h.get("to") is None:
                        h["to"] = now
                        h["reason"] = reason
                binding["history"] = hist
                binding["sticky_ip"] = None
                binding["bound_since"] = None
        shelf.save_pool(pool)
        shelf.save_bindings(bindings)
        shelf.append_log("burn", addr=addr, reason=reason, released=channels)
        return {"released": channels}


def assign_region(channel_key, region, bindings=None) -> dict:
    """LAW 3 — set/lock a channel's region (creates a stub binding).

    Region is permanent once set; a different later region is refused
    with {"error": "region_locked", "current": ...}.
    """
    key = _normalize_key(channel_key)
    region = (region or "").strip()
    if not region:
        return {"error": "bad_region"}
    if bindings is None:
        bindings = shelf.load_bindings()

    with shelf.store_lock():
        binding = bindings.get(key, {})
        current = binding.get("region")
        if current and current != region:
            return {"error": "region_locked", "current": current}

        if current == region:
            return {"channel": key, "region": region, "locked": True,
                    "already": True}

        binding["region"] = region
        binding.setdefault("history", [])
        bindings[key] = binding
        shelf.save_bindings(bindings)
        shelf.append_log("assign_region", channel=key, region=region)
        return {"channel": key, "region": region, "locked": True}


def needs_harvest(region, demand=10, pool=None) -> bool:
    """LAW 7 — is this shelf starving? True when free capacity < demand.

    Free capacity = sum of free channel slots across the region's free
    gold exits (an exit with 3 slots left contributes 3).
    """
    if pool is None:
        pool = shelf.load_pool()
    free_slots = 0
    for e in shelf.free_ips(pool, region):
        free_slots += shelf.CAP - len(e.get("channels", []))
    return free_slots < demand

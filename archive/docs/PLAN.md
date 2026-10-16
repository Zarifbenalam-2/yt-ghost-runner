# IP SHELF — System Plan (locked 2026-09-10, corrected hierarchy)

**The hierarchy (never invert this again):**
1. **MAIN PROJECT** = `yt-channel-automation` — creates brand channels under one
   Gmail (cap ~100) and warms them up (watch / like / subscribe / comment, human-paced,
   single user). It is the reason everything else exists.
2. **THE FIX** = this IP shelf — because warm-up was running on the home IP, Google
   flagged it, and the user's personal YouTube was in danger. The shelf gives every
   channel session a sticky gold IP so home IP never touches automation traffic again.
3. **SUPPLY** = the gold hunt machine (`FRee Gold IP\extracted\proxy-machine\`) — a
   side branch whose only job is feeding graded gold IPs onto the shelves.

**One-line:** A region-shelf storage and binding system for **graded gold IPs**, where each
channel (brand account) holds **one sticky gold IP for life**, and a watch run is
`one YouTube link → one set of 10 channels → each draws from its own region's shelf`.

Location: `D:\New folder (3)\streamstress-full-project\yt-channel-automation\ipshelf\`

---

## 1. The laws (user decisions — do not change)

1. **Verified-only display.** Only IPs that survived the FULL pipeline (harvest → TCP
   pre-filter → gold-gate rank → **grading**) are counted/shown as holdings. Discovery-stage
   numbers (e.g. "211 found") are working data, never reported as results.
2. **One channel = one sticky gold IP, for life.** Not per-session, not per-Gmail rotation.
   The IP is replaced ONLY when it dies or fails a gold re-check.
3. **Region lock.** A channel without a region picks one at its first task (user chooses:
   US, UK, tier-1, …). That choice is saved **forever**. After that, the channel only
   ever draws from that region's shelf. No location drift, ever.
4. **Cap: max 5 channels per IP.** One IP can serve up to 5 different channels simultaneously.
   Deliberately small footprint so Google sees nothing unusual.
5. **IPs never change shelves.** An IP is stored on the shelf of the region it was born in
   (its geo from grading).
6. **Quick gold gate before bind.** When an IP is drawn from a shelf, a fresh quick gold
   check runs; only on pass does the bind happen. Fail → next-best IP on same shelf.
7. **Shelf starvation → auto-harvest.** If a shelf cannot cover the channels needing it,
   a harvest session fires (targeting that region's sources) and the run **waits**.
8. **Next-day reuse.** Same channels + new link → same sticky IPs (re-gated). IP still
   gold → same IP. IP dead → burned, same shelf, new IP.
9. **Refresh/Sweep button.** User-triggered (and periodic): re-check every stored gold IP,
   **burn + delete** dead/non-gold ones, then run a fresh harvest to top numbers back up.

## 2. Watch-run flow (one link → 10 channels)

1. User pastes a YouTube link; system pulls video ID + duration.
2. User picks the set — **10 channels = one set**. Random 10 by default, or hand-pick.
3. Per channel: no region yet → user picks now → locked forever + saved.
4. Channel draws from its region shelf: sticky IP if alive → else best free gold IP
   (score-sorted, cap 5 ch/IP, not already bound to this channel).
5. New draws run a **quick gold gate**. Pass → bind sticky (record since-date).
   Fail → next-best; shelf exhausted → auto-harvest, run waits, retry.
6. Execute the watch session per channel over its bound IP (existing warm-up flow:
   switch to handle → watch link at 55–95% depth → like 30% / subscribe 15% / comment 10%).
7. Persist everything: channel→region, channel→IP, IP→channels, IP→gold status.

## 3. Integration into the main engine (code-confirmed)

The engine already has every integration point ready (read from the actual code):

- `engine/worker.py:58-69` — currently resolves proxy via `lanes.lane_for_account()`:
  **this is the exact line the shelf replaces.** `proxy = shelf.draw_for_channel(ch)`
  instead of the Tor lane. Everything else in the worker (CaptchaWall cooldown,
  ProxyDead reschedule, verify gate, stale account) stays as-is.
- `engine/browser.py:41` — `open_proxied_arg(p, proxy_server, account_id)` already
  launches CloakBrowser with `--proxy-server=<socks5/http>`, persistent profile per account.
  The shelf only needs to hand it `socks5://107.167.18.122:443`-style URLs.
- `engine/db.py` — shared sqlite (`prisma/db/custom.db`, WAL). The shelf adds its own
  tables here OR keeps `ipshelf/data/*.json`. Decision for next session: JSON files for
  pool/bindings (simple, matches existing dashboard state style) — engine reads via a
  thin `ipshelf` module import so the worker doesn't parse JSON itself.
- `engine/warmup.py:24` — `run_session(acc, channel, payload, proxy)` takes the proxy as
  an argument: no signature change needed, just a new supplier for the argument.
- `engine/scheduler.py` — unchanged; it already claims tasks and calls the worker.
- Channels are **Channel rows in the shared DB**, not Gmail addresses: shelf bindings
  key on `Channel.id`/`handle` (e.g. `@ZarifTestChannel01`), matching the cap of ~100
  channels per Gmail and the "10 channels = one set" model.

## 4. Data model (file-based JSON, atomic writes)

`ipshelf/data/shelf_pool.json` — the gold inventory:
```
{ "updated": "...", "exits": [ {
    "addr": "107.167.18.122:443", "proto": "SOCKS",
    "geo": {...}, "cc": "US", "tier": "T1", "type": "datacenter",
    "score": 88.2, "latency_ms": 808, "bandwidth_mbps": 8.73,
    "playability": "OK", "first_seen": "...", "last_gold_ok": "...",
    "channels": ["@ZarifTestChannel01"],   // ≤ 5, by channel id/handle
    "status": "gold" | "burned"
} ] }
```
`ipshelf/data/bindings.json` — channel side:
```
{ "@ZarifTestChannel01": { "region": "US",          // locked forever once set
               "sticky_ip": "107.167.18.122:443",
               "bound_since": "...", "history": [...] } }
```
Shelves are derived views (group pool by `cc`/tier) — no separate shelf file to drift.

##  keyed on Channel handle

## 5. Modules

| File | Job |
|---|---|
| `shelf.py` | Pool + bindings store, load/save, shelf queries (free IP, cap check, best-by-score) |
| `assigner.py` | The draw: region lookup → best free IP → quick gold gate → sticky bind; starvation detection |
| `sweeper.py` | Burn pass (re-gate everything, delete dead), top-up trigger, periodic schedule |
| `harvest_bridge.py` | Isolated COPY of the hunt pipeline (harvest → pre-filter → rank → grade), filtered by target region, feeding `shelf_pool.json` |
| `gate.py` | Single-IP quick gold check (YouTube watch page + playabilityStatus OK) |
| `server.py` | Dashboard HTTP server (pattern of existing `dashboard.py`: `ThreadingHTTPServer` + JSON endpoints) |
| `ui/` | Warehouse Dark (locked design) wired to `/pool`, `/bindings`, `/sweep`, `/run` |
| `run_watch.py` | Orchestrates link → 10 channels → shelf draw → hands each channel+IP to `engine.warmup.run_session` |

## 6. Build order

- **Phase 1:** `shelf.py` + `assigner.py` + `gate.py` + `sweeper.py` + Warehouse Dark UI
  wired to real JSON state (seed from `goldbank/standing_pool.json` → 42 graded gold).
- **Phase 2:** swap `engine/worker.py` proxy resolution from Tor lane → shelf draw;
  `run_watch.py` one-link/10-channel run orchestration; `harvest_bridge.py`
  (copy pipeline into `ipshelf/hunt/`, strip personal paths — zip-portable rule).
- **Phase 3:** periodic auto-sweep, burn log, run history, starvation auto-harvest
  background loop.

## 7. Engineering constraints carried from last session

- Windows: Python `WindowsProactorEventLoopPolicy` (512-fd select cap), no `python3` alias,
  curl must write to a real temp file (not /dev/null), hunt PAR=120.
- No hardcoded personal Python paths — anyone must be able to run it.
- Fully isolated system; pipeline scripts are copied, never referenced across folders.
- Gold standard = proxy fetched the real YouTube watch page AND got `playabilityStatus: OK`
  (YouTube's own live IP-reputation oracle; CloakBrowser runs proved it end-to-end with
  actual video playback).

## 8. Real state (as of 2026-09-10, from actual files)

- Gold pool: **42 graded gold alive** in `goldbank/standing_pool.json`.
- Real distribution: **US only 2** (107.167.18.122:443 LA 88.2, 107.181.252.58:1081
  Ogden 79.2) — T1 others: KR, FI, FR (1 each); rest: MX, PK, VN, ID, BD, HK… 37 total.
  → A 10-channel US set needs exactly max cap (2×5). **US harvest is day-one priority.**
- Real channels (from `channels_inventory.json` / shared DB): @ZarifBenAlam (personal,
  never automated), @ZarifTestChannel01, @ZarifTestChannel01-b7t, @FreshChannel01.
- Real constraint the shelf must respect: policy.yaml caps — subs 2/day, comments 2/day,
  likes 6/day per account; watch depth 55–95%; sessions 1–2/day; off-day 15%.

## 9. Status

- [x] Hierarchy corrected: YT automation = main project, shelf = its IP wing, hunt = supply
- [x] Designs: Warehouse Dark **skin locked**; content rebuilt around the real system
- [x] PLAN.md corrected with code-confirmed integration points
- [ ] Phase 1 implementation (next session)
- [ ] Phase 2 worker integration + watch runs + harvest bridge
- [ ] Phase 3 polish

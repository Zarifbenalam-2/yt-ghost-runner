# ipshelf/hunt — the self-contained gold hunt machine

A **copied** (not referenced) instance of the proxy-machine pipeline that
harvests free public proxies, filters them against YouTube's own
`playabilityStatus` oracle, grades the survivors, and feeds the graded exits
into the shelf pool. **The law:** pipeline scripts are copied, never
referenced across folders — this directory has zero runtime dependency on
`FRee Gold IP/`.

## What runs here

```
  [1] HUNT     proxy_hunt.sh / fast_hunt.py
              30+ verified public proxy lists -> raw TCP CONNECT pre-filter
              -> proxyhunt/winners.txt (proxies that reached youtube.com)
                       |
  [2] RANK     proxy_rank.sh
              fetch the REAL watch page per winner, read
              playabilityStatus.status == OK  ->  GOLD exits
              -> proxyhunt/rank_results.txt
                       |
  [3] GRADE   exitpool.py grade
              egress IP (ipify) -> geo (ip-api batch) -> tier -> type ->
              latency (gstatic 204) -> bandwidth (Cloudflare 8MB) ->
              playability -> GitHub -> composite 0-100 score
              -> proxyhunt/pool.json
                       |
  [4] MERGE   harvest.py (this bridge) + ipshelf.core.shelf
              merge_exits() into shelf_pool.json — existing channels and
              first_seen kept, new exits get channels=[]; bindings are
              never disturbed by gold supply
```

`hunt_cycle.py` is an alternative cron-style entry (HUNT+RANK+GRADE plus its
own standing-gold merge with prune into
`proxyhunt/goldbank/standing_pool.json`); `refresh_pool.sh` is the
stage-1..3 orchestrator the bridge calls.

## How to run

From the **yt-channel-automation** root, with the venv python:

```
venv/Scripts/python.exe -m ipshelf.hunt.harvest [--par 120] [--timecap 1800] [--no-prune]
```

- streams all pipeline output to stdout **and** appends it to
  `ipshelf/data/harvest.log` (or `$IPSHELF_DATA/harvest.log` if set),
- writes `ipshelf/data/harvest_status.json` (`running: true` at start, final
  state with `added`/`refreshed`/`total_gold`/`rc`/per-`cc` regions at end),
- exit code = pipeline rc.

### Manual debugging (Git Bash), stage by stage

```bash
cd ipshelf/hunt

bash proxy_hunt.sh                       # HUNT (curl-based, ~6-20 min)
PYBIN=../../venv/Scripts/python.exe      # or:
"../../venv/Scripts/python.exe" fast_hunt.py both 120 8   # fast async HUNT
bash proxy_rank.sh                       # RANK -> rank_results.txt
"../../venv/Scripts/python.exe" exitpool.py grade        # GRADE -> pool.json
"../../venv/Scripts/python.exe" exitpool.py top          # champions table
"../../venv/Scripts/python.exe" exitpool.py retest       # survival re-check
PYBIN="../../venv/Scripts/python.exe" bash refresh_pool.sh   # full 1..3
"../../venv/Scripts/python.exe" -m ipshelf.hunt.hunt_cycle   # + standing bank
```

## Env knobs

| Var | Default | Meaning |
|---|---|---|
| `PAR` | 120 | parallelism (Windows-safe sweet spot; `--par` sets it via the bridge) |
| `HTTP_CAP` | 3000* | max HTTP candidates tested (*4000 in raw proxy_hunt.sh) |
| `SOCKS_CAP` | 1200 | max SOCKS5 candidates |
| `SOCKS4_CAP` | 800 | max SOCKS4 candidates |
| `TIMEOUT` | 8 | per-test seconds |
| `PYBIN` | – | python the pipeline scripts must use (bridge sets it to the venv python) |
| `CURL_BIN` | – | explicit curl path (else Git-Bash mingw64 curl is preferred) |
| `IPSHELF_DATA` | `ipshelf/data` | data dir for harvest.log / harvest_status.json |

## Windows notes (all battle-tested, do not "fix")

- **mingw64 curl**: `exitpool.py` prefers Git-Bash/MSYS curl over
  `C:\Windows\System32\curl.exe` (the latter breaks socks + /dev/null).
  Keep that detection block as-is; set `CURL_BIN` to override.
- **Proactor loop**: `fast_hunt.py` forces
  `WindowsProactorEventLoopPolicy` — the default selector loop caps at 512
  fds and crashes with thousands of sockets in flight.
- **No `python3` alias on Windows**: always use the venv python
  (`venv/Scripts/python.exe`). The bridge passes it down as `PYBIN`;
  the shell scripts only fall back to `python`/`py -3` detection when
  `PYBIN` is unset (the Windows-Store stub must be avoided — it prints an
  ad and exits 0).

## Where outputs live (all regeneratable — don't commit the big dumps)

| Path | What |
|---|---|
| `proxyhunt/*_all.txt` | deduped harvested candidate lists |
| `proxyhunt/winners.txt` | proxies that served YouTube 200 |
| `proxyhunt/rank_results.txt` | playability verdicts (OK = GOLD) |
| `proxyhunt/pool.json` | **graded pool — what the bridge merges** |
| `proxyhunt/goldbank/standing_pool.json` | standing bank (hunt_cycle) |
| `../data/harvest.log` / `../data/harvest_status.json` | bridge log + status |

## Honest limitation: no geo-targeting at harvest

Free public proxy lists are global — they cannot be filtered by country at
harvest time. **All gold is harvested globally**; region selection happens
at **draw time** (the core assigner filters the merged pool by `cc`). If the
US shelf runs thin, the fix is running more/frequent harvests and letting
the assigner pick US exits from what lands — not targeting a region during
harvest (impossible with these sources).

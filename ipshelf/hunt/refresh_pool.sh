#!/usr/bin/env bash
# ============================================================================
# ipshelf/hunt — self-contained copy of the gold hunt machine (no dependency
# on FRee Gold IP). Adapted: honors PYBIN env var for the venv python before
# falling back to python detection; exitpool invocation only.
# ============================================================================
# refresh_pool.sh — THE WHOLE MACHINE IN ONE COMMAND
# ============================================================================
# Runs the full pipeline, top to bottom:
#   1. HUNT   : download fresh public proxy lists, blast them at youtube.com,
#               keep the ones that serve HTTP 200 (winners)
#   2. RANK   : fetch the watch page through each winner, read Google's
#               playabilityStatus -> keep only OK ("GOLD") exits
#   3. GRADE  : for each GOLD exit measure: real egress IP, country, tier,
#               latency, download speed, YouTube playability, GitHub access
#               -> composite 0-100 score -> pool.json
#   4. REPORT : print the champions table
#
# Run it whenever you want fresh exits. Typical full run: 5-15 minutes.
#
# CRON example (refresh every 2 hours on your own server):
#   0 */2 * * * /path/to/refresh_pool.sh >> /path/to/refresh.log 2>&1
#
# Note on waiting: proxy lists update hourly, and dead exits free up Google
# flags over time — so re-running after 30-60 min DOES surface new IPs.
# ============================================================================
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "==============================================================="
echo " EXIT POOL REFRESH  —  $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "==============================================================="

echo; echo "[1/4] HUNT — fresh proxy lists -> youtube.com winners"
bash "$HERE/proxy_hunt.sh"

echo; echo "[2/4] RANK — winners -> playabilityStatus -> GOLD exits"
bash "$HERE/proxy_rank.sh"

echo; echo "[3/4] GRADE — GOLD exits -> geo/tier/speed/playability/GitHub -> score"
# ipshelf: PYBIN (venv python) wins if set; else find a REAL python
# (Windows Store stub prints an ad and exits 0 — verify by running code)
if [ -n "${PYBIN:-}" ]; then
  PY="$PYBIN"
elif command -v python >/dev/null 2>&1 && python -c "print(1)" 2>/dev/null | grep -q 1; then
  PY="$(command -v python)"
elif command -v py >/dev/null 2>&1 && py -3 -c "print(1)" 2>/dev/null | grep -q 1; then
  PY="py -3"
else
  PY="$(command -v python3)"
fi
"$PY" "$HERE/exitpool.py" grade

echo; echo "[4/4] TOP — current champions"
"$PY" "$HERE/exitpool.py" top

echo; echo "==============================================================="
echo " DONE. Live pool: $HERE/proxyhunt/pool.json"
echo " Champions survive longest — re-run in a few hours for fresh blood."
echo "==============================================================="

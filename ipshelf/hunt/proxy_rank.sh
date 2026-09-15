#!/usr/bin/env bash
# ============================================================================
# ipshelf/hunt — self-contained copy of the gold hunt machine (no dependency
# on FRee Gold IP). Adapted: header comment only; logic unchanged.
# ============================================================================
# proxy_rank.sh — rank winning proxies by YouTube player playability status
# ============================================================================
# For each proxy that served the watch page (winners.txt), fetch the page HTML
# and extract ytInitialPlayerResponse.playabilityStatus.status:
#   OK               -> GOLD (page + player allowed, best browser candidates)
#   LOGIN_REQUIRED   -> soft bot-wall ("Sign in to confirm you're not a bot")
#   other/missing    -> consent page / error / unusable
# ============================================================================
set -u
# portable: live next to proxy_hunt.sh output, wherever this script lives
DIR="$(cd "$(dirname "$0")" && pwd)/proxyhunt"
cd "$DIR"

TEST_URL="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PAR="${PAR:-120}"          # raised from 40: rank is network-bound, not CPU-bound
TIMEOUT=14

rank_one() {
  local kind="$1" p="$2"
  local html status
  if [ "$kind" = "HTTP" ]; then
    html=$(curl -s -x "http://$p" --max-time $TIMEOUT "$TEST_URL" 2>/dev/null)
  else
    html=$(curl -s --socks5-hostname "$p" --max-time $TIMEOUT "$TEST_URL" 2>/dev/null)
  fi
  if [ -z "$html" ]; then echo "DEAD|$kind|$p"; return; fi
  status=$(printf '%s' "$html" | grep -o '"playabilityStatus":{"status":"[A-Z_]*' | head -1 | sed 's/.*status":"//')
  if [ -z "$status" ]; then
    # maybe consent interstitial
    if printf '%s' "$html" | grep -q "consent.youtube.com\|Before you continue"; then
      echo "CONSENT|$kind|$p"
    else
      echo "NOSTATUS|$kind|$p"
    fi
  else
    echo "$status|$kind|$p"
  fi
}
export -f rank_one
export TEST_URL TIMEOUT

echo "[*] ranking all winners..."
awk '{print $1, $2}' winners.txt | sed 's/_WIN /|/' | tr -d '\n' > /dev/null  # noop
grep -o '^[A-Z_]*_WIN [0-9.:]*' winners.txt | sed 's/_WIN /|/' | while IFS='|' read -r kind p; do
  echo "$kind $p"
done > candidates.txt

: > rank_results.txt
while IFS=' ' read -r kind p; do
  echo "$kind $p"
done < candidates.txt | xargs -P $PAR -n2 bash -c 'rank_one "$@"' _ > rank_results.txt

echo "[*] RANKING SUMMARY:"
cut -d'|' -f1 rank_results.txt | sort | uniq -c | sort -rn
echo
echo "[*] GOLD (playabilityStatus OK) exits:"
grep '^OK|' rank_results.txt | head -25
echo "[*] total OK: $(grep -c '^OK|' rank_results.txt || true)"

#!/usr/bin/env bash
# ============================================================================
# ipshelf/hunt — self-contained copy of the gold hunt machine (no dependency
# on FRee Gold IP). Adapted: header comment + PYBIN honored in find_py;
# harvesting sources and funnel logic unchanged.
# ============================================================================
# proxy_hunt.sh v3 — 30+ doc-verified sources, google-passed priority, socks4
# ============================================================================
# v3 (R-E10 research applied): every URL doc-verified (README) + live-verified.
# Parser fix: strips scheme:// prefixes (proxifly/dpangestuw) and :Country
# suffixes (zloi) that the old anchored regex silently DROPPED.
# New: GeoNode API (google-flag JSON), mauricegift JSON, SOCKS4 testing,
# 17 bonus sources from monosans' own config.
# ============================================================================
set -u
# portable: work in a "proxyhunt" folder next to this script, wherever it lives
DIR="$(cd "$(dirname "$0")" && pwd)/proxyhunt"
mkdir -p "$DIR"; cd "$DIR"

TEST_URL="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PAR="${PAR:-120}"               # parallelism (override: PAR=200 bash proxy_hunt.sh)
TIMEOUT="${TIMEOUT:-8}"         # per-test seconds
HTTP_CAP="${HTTP_CAP:-4000}"    # max HTTP candidates (raise for go-all-out)
SOCKS_CAP="${SOCKS_CAP:-2200}"  # max SOCKS5 candidates
SOCKS4_CAP="${SOCKS4_CAP:-1500}"
ZEV_SAMPLE="${ZEV_SAMPLE:-3000}" # sample from the huge unchecked dump

IPPORT='^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}:[0-9]+$'
LOOSE='[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}:[0-9]+'

# find a REAL python (Windows Store stub prints an ad and exits 0 — verify by
# running code). ipshelf: PYBIN (venv python) wins if set.
find_py() {
  local c
  if [ -n "${PYBIN:-}" ] && "$PYBIN" -c "print(1)" >/dev/null 2>&1; then
    echo "$PYBIN"
    return 0
  fi
  for c in python3 python py; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "print(1)" 2>/dev/null | grep -q 1; then
      case "$c" in py) echo "py -3";; *) command -v "$c";; esac
      return 0
    fi
  done
  for c in "$HOME/AppData/Local/Programs/Python/Python312/python.exe" \
           "$HOME/AppData/Local/Programs/Python/Python311/python.exe" \
           /c/Python312/python.exe /c/Python311/python.exe; do
    [ -f "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}
PY="$(find_py)" || { echo "FATAL: no real python found (set PYBIN=<venv>/Scripts/python.exe)"; exit 1; }

echo "[*] harvesting 30+ verified proxy list sources (parallel download)..."
fetch_bg() {
  local name="${1%%|*}" url="${1#*|}"
  ( curl -sL --max-time 25 "$url" -o "$name" 2>/dev/null || true ) &
}
fetch_bg "http_monosans.txt|https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"
fetch_bg "socks5_monosans.txt|https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"
fetch_bg "http_speedx.txt|https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"
fetch_bg "socks5_speedx.txt|https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"
fetch_bg "http_proxyscrape.txt|https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all"
fetch_bg "socks5_proxyscrape.txt|https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all"
fetch_bg "http_spys.txt|https://spys.me/proxy.txt"
fetch_bg "socks5_spys.txt|https://spys.me/socks.txt"
fetch_bg "http_openproxylist.txt|https://openproxylist.xyz/http.txt"
fetch_bg "socks5_openproxylist.txt|https://openproxylist.xyz/socks5.txt"
fetch_bg "socks5_hookzof.txt|https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"
fetch_bg "http_clarketm.txt|https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"
fetch_bg "all_zevtyardt.txt|https://raw.githubusercontent.com/zevtyardt/proxy-list/main/all.txt"
fetch_bg "http_proxifly.txt|https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"
fetch_bg "https_proxifly.txt|https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt"
fetch_bg "socks4_proxifly.txt|https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt"
fetch_bg "socks5_proxifly.txt|https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt"
fetch_bg "all_proxifly.txt|https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"
fetch_bg "http_sunny.txt|https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"
fetch_bg "socks5_sunny.txt|https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt"
fetch_bg "socks4_sunny.txt|https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks4_proxies.txt"
fetch_bg "https_zloi.txt|https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt"
fetch_bg "connect_zloi.txt|https://raw.githubusercontent.com/zloi-user/hideip.me/main/connect.txt"
fetch_bg "socks4_zloi.txt|https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks4.txt"
fetch_bg "socks5_zloi.txt|https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt"
fetch_bg "http_vakhov.txt|https://vakhov.github.io/fresh-proxy-list/http.txt"
fetch_bg "socks4_vakhov.txt|https://vakhov.github.io/fresh-proxy-list/socks4.txt"
fetch_bg "socks5_vakhov.txt|https://vakhov.github.io/fresh-proxy-list/socks5.txt"
fetch_bg "all_vakhov.txt|https://vakhov.github.io/fresh-proxy-list/proxylist.txt"
fetch_bg "http_obcbo.txt|https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt"
fetch_bg "socks5_obcbo.txt|https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks5.txt"
fetch_bg "http_tuan.txt|https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/http.txt"
fetch_bg "http_dpang.txt|https://raw.githubusercontent.com/dpangestuw/Free-PROXY/main/http_proxies.txt"
fetch_bg "http_databay.txt|https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt"
fetch_bg "socks5_databay.txt|https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt"
fetch_bg "http_hproxy.txt|https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt"
fetch_bg "socks5_hproxy.txt|https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/socks5.txt"
fetch_bg "http_anonym.txt|https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt"
fetch_bg "socks5_anonym.txt|https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt"
fetch_bg "http_ebrasha.txt|https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/http-proxy-list-by-EbraSha.txt"
fetch_bg "socks5_ebrasha.txt|https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/socks5-proxy-list-by-EbraSha.txt"
fetch_bg "http_dinoz.txt|https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/http.txt"
fetch_bg "socks5_dinoz.txt|https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/socks5.txt"
fetch_bg "http_iplocate.txt|https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt"
fetch_bg "socks5_iplocate.txt|https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks5.txt"
fetch_bg "http_maurice.json|https://raw.githubusercontent.com/mauricegift/free-proxies/master/files/http.json"
fetch_bg "socks5_maurice.json|https://raw.githubusercontent.com/mauricegift/free-proxies/master/files/socks5.json"
( curl -sL --max-time 30 "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc" -o geonode1.json 2>/dev/null || true ) &
( curl -sL --max-time 30 "https://proxylist.geonode.com/api/proxy-list?limit=500&page=2&sort_by=lastChecked&sort_type=desc" -o geonode2.json 2>/dev/null || true ) &
( curl -sL --max-time 30 "https://proxylist.geonode.com/api/proxy-list?limit=500&page=3&sort_by=lastChecked&sort_type=desc" -o geonode3.json 2>/dev/null || true ) &
wait
echo "    downloads done."

# --- JSON sources -> plain ip:port (+ geonode google-flag bonus) ---
"$PY" - <<'PYEOF' 2>/dev/null || true
import json, glob
def dump(items, fn):
    with open(fn, "w") as f:
        f.write("\n".join(items) + ("\n" if items else ""))
http, socks, gpass = [], [], []
for fn in glob.glob("geonode*.json"):
    try: d = json.load(open(fn))
    except Exception: continue
    for p in d.get("data", []):
        try: line = f"{p['ip']}:{int(p['port'])}"
        except Exception: continue
        if "http" in p.get("protocols", []): http.append(line)
        if "socks5" in p.get("protocols", []): socks.append(line)
        if p.get("google"): gpass.append(line)
for fn, arr in (("http_maurice.json", http), ("socks5_maurice.json", socks)):
    try:
        d = json.load(open(fn))
        for p in (d.get("proxies") or d.get("data") or []):
            if isinstance(p, dict):
                try: arr.append(f"{p['ip']}:{int(p['port'])}")
                except Exception: pass
            elif isinstance(p, str) and ":" in p:
                arr.append(p.split("//")[-1])
    except Exception: pass
dump(sorted(set(http)), "geonode_http.txt")
dump(sorted(set(socks)), "geonode_socks.txt")
dump(sorted(set(gpass)), "geonode_google.txt")
PYEOF
for f in http_monosans.txt socks5_monosans.txt http_speedx.txt socks5_speedx.txt \
         http_proxyscrape.txt socks5_proxyscrape.txt http_spys.txt socks5_spys.txt \
         http_openproxylist.txt socks5_openproxylist.txt http_clarketm.txt http_obcbo.txt \
         socks5_obcbo.txt http_tuan.txt http_dpang.txt http_dinoz.txt socks5_dinoz.txt \
         geonode_http.txt geonode_socks.txt http_proxifly.txt all_proxifly.txt; do
  [ -f "$f" ] && echo "    $f: $(grep -cE "$LOOSE" "$f" 2>/dev/null || echo 0)"
done

# --- spys.me "+" = Google-passed. geonode google:true = Google-passed. ---
: > google_passed_http.txt; : > google_passed_socks.txt
[ -f http_spys.txt ]   && awk '$NF=="+"{print $1}' http_spys.txt   2>/dev/null | grep -E "$IPPORT" >> google_passed_http.txt
[ -f socks5_spys.txt ] && awk '$NF=="+"{print $1}' socks5_spys.txt 2>/dev/null | grep -E "$IPPORT" >> google_passed_socks.txt
[ -f geonode_google.txt ] && cat geonode_google.txt >> google_passed_http.txt
sort -u google_passed_http.txt -o google_passed_http.txt
sort -u google_passed_socks.txt -o google_passed_socks.txt
echo "[*] Google-passed pre-vouched: $(wc -l < google_passed_http.txt) http + $(wc -l < google_passed_socks.txt) socks"

# --- normalize + merge (FIX: strips scheme:// and :Country suffixes) ---
norm() { grep -hE "$LOOSE" "$@" 2>/dev/null | sed -E 's|^[a-zA-Z][a-zA-Z0-9+.-]*://||' | awk '{print $1}' | awk -F: '{print $1":"$2}' | grep -E "$IPPORT"; }
norm http_*.txt https_*.txt connect_*.txt all_vakhov.txt all_proxifly.txt geonode_http.txt 2>/dev/null | sort -u > http_all.txt
norm socks5_*.txt geonode_socks.txt 2>/dev/null | sort -u > socks_all.txt
grep -E "$IPPORT" all_zevtyardt.txt 2>/dev/null | head -"$ZEV_SAMPLE" | awk '{print $1}' >> socks_all.txt
sort -u socks_all.txt -o socks_all.txt
norm socks4_*.txt 2>/dev/null | sort -u > socks4_all.txt
echo "[*] unique harvested: HTTP $(wc -l < http_all.txt) + SOCKS5 $(wc -l < socks_all.txt) + SOCKS4 $(wc -l < socks4_all.txt)"

cat google_passed_http.txt http_all.txt | awk '!seen[$0]++' | head -$HTTP_CAP > http_test.txt
cat google_passed_socks.txt socks_all.txt | awk '!seen[$0]++' | head -$SOCKS_CAP > socks_test.txt
sort -u socks4_all.txt | head -$SOCKS4_CAP > socks4_test.txt
echo "[*] testing queue (google-passed first): HTTP $(wc -l < http_test.txt) + SOCKS5 $(wc -l < socks_test.txt) + SOCKS4 $(wc -l < socks4_test.txt)"

check_http() {
  local p="$1"; local code
  code=$(curl -s -x "http://$p" --max-time $TIMEOUT -o /dev/null -w "%{http_code}" "$TEST_URL" 2>/dev/null)
  if [ "$code" = "200" ]; then echo "HTTP_WIN $p code=200"; fi
}
check_socks() {
  local p="$1"; local code
  code=$(curl -s --socks5-hostname "$p" --max-time $TIMEOUT -o /dev/null -w "%{http_code}" "$TEST_URL" 2>/dev/null)
  if [ "$code" = "200" ]; then echo "SOCKS_WIN $p code=200"; fi
}
check_socks4() {
  local p="$1"; local code
  code=$(curl -s --socks4a "$p" --max-time $TIMEOUT -o /dev/null -w "%{http_code}" "$TEST_URL" 2>/dev/null)
  if [ "$code" = "200" ]; then echo "SOCKS4_WIN $p code=200"; fi
}
export -f check_http check_socks check_socks4
export TEST_URL TIMEOUT

echo "[*] testing HTTP proxies (parallel $PAR)..."
xargs -a http_test.txt -P $PAR -I{} bash -c 'check_http "$@"' _ {} > winners_raw.txt 2>/dev/null
echo "[*] testing SOCKS5 proxies..."
xargs -a socks_test.txt -P $PAR -I{} bash -c 'check_socks "$@"' _ {} >> winners_raw.txt 2>/dev/null
echo "[*] testing SOCKS4 proxies..."
xargs -a socks4_test.txt -P $PAR -I{} bash -c 'check_socks4 "$@"' _ {} >> winners_raw.txt 2>/dev/null

sort winners_raw.txt | tee winners.txt > /dev/null
echo "[*] funnel: harvested HTTP $(wc -l < http_all.txt) + SOCKS5 $(wc -l < socks_all.txt) + SOCKS4 $(wc -l < socks4_all.txt) | tested $(($(wc -l < http_test.txt)+$(wc -l < socks_test.txt)+$(wc -l < socks4_test.txt))) | winners: $(grep -c WIN winners_raw.txt || true)"

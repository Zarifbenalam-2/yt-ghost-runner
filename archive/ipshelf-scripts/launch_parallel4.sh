#!/bin/bash
# 4 parallel headed engagement sessions, 10s stagger — old channels,
# full spec: watch Ba3ku528N18 -> like+sub+comment -> random rec -> like -> close
cd "D:/New folder (3)/streamstress-full-project/yt-channel-automation" || exit 1
PY="$PWD/venv/Scripts/python.exe"
URL="https://www.youtube.com/watch?v=Ba3ku528N18"

"$PY" ipshelf/engage_test.py --handle "@ScaleTest01"     --url "$URL" --headful --profile-alias parallel-1 > ipshelf/data/engage_par1.log 2>&1 &
P1=$!
echo "launched @ScaleTest01 (pid $P1)"
sleep 10
"$PY" ipshelf/engage_test.py --handle "@ScaleTest01-d3r" --url "$URL" --headful --profile-alias parallel-2 > ipshelf/data/engage_par2.log 2>&1 &
P2=$!
echo "launched @ScaleTest01-d3r (pid $P2)"
sleep 10
"$PY" ipshelf/engage_test.py --handle "@ScaleTest02"     --url "$URL" --headful --profile-alias parallel-3 > ipshelf/data/engage_par3.log 2>&1 &
P3=$!
echo "launched @ScaleTest02 (pid $P3)"
sleep 10
"$PY" ipshelf/engage_test.py --handle "@ScaleTest03"     --url "$URL" --headful --profile-alias parallel-4 > ipshelf/data/engage_par4.log 2>&1 &
P4=$!
echo "launched @ScaleTest03 (pid $P4)"

wait $P1 $P2 $P3 $P4
echo "=== ALL 4 PARALLEL SESSIONS FINISHED ==="

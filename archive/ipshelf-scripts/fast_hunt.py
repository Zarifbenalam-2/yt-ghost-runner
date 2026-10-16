#!/usr/bin/env python3
# ipshelf/hunt — self-contained copy of the gold hunt machine (no dependency
# on FRee Gold IP). Adapted: header comment only; Proactor loop + logic
# unchanged (battle-tested Windows fixes).
"""fast_hunt.py — async one-process proxy tester (Windows-native, stdlib-only).

Replaces the curl-per-proxy hunt phase: one Python process, asyncio event
loop, raw TCP CONNECT for HTTP proxies (no curl spawn per candidate).
~5-15x faster than PAR=60 curl on the same box because there is no process
startup cost and thousands of sockets can be in flight at once.

Usage:
  python fast_hunt.py http|socks|both [workers] [timeout]
  python fast_hunt.py both 400 8        # test everything harvested
"""
import asyncio
import glob
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HUNT = os.path.join(HERE, "proxyhunt")

TEST_HOST = "www.youtube.com"
TEST_PORT = 443
IPPORT = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d+)$")


async def try_http(addr, sem, timeout, out, stats):
    async with sem:
        m = IPPORT.match(addr)
        if not m:
            return
        host, port = m.group(1), int(m.group(2))
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout)
            req = (f"CONNECT {TEST_HOST}:{TEST_PORT} HTTP/1.1\r\n"
                   f"Host: {TEST_HOST}:{TEST_PORT}\r\n\r\n")
            writer.write(req.encode())
            await asyncio.wait_for(writer.drain(), timeout)
            line = await asyncio.wait_for(reader.readline(), timeout)
            ok = line.startswith(b"HTTP/1.") and (b" 200" in line)
            writer.close()
            if ok:
                stats["win"] += 1
                out.append(f"HTTP_WIN {addr} code=200")
        except Exception as exc:
            stats.setdefault("errors", {})
            k = type(exc).__name__ + (f":{exc.args[0]}" if exc.args and isinstance(exc.args[0], str) and "1005" in str(exc.args[0]) else "")
            stats["errors"][k] = stats["errors"].get(k, 0) + 1
        finally:
            stats["done"] += 1
            if stats["done"] % 2000 == 0:
                print(f"    {stats['done']}/{stats['total']} done, {stats['win']} wins", flush=True)


async def try_socks(addr, sem, timeout, out, stats):
    async with sem:
        m = IPPORT.match(addr)
        if not m:
            return
        host, port = m.group(1), int(m.group(2))
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout)
            # SOCKS5 greeting: ver5, 1 method, no-auth(0)
            writer.write(b"\x05\x01\x00")
            await asyncio.wait_for(writer.drain(), timeout)
            resp = await asyncio.wait_for(reader.readexactly(2), timeout)
            writer.close()
            if resp[0] == 5 and resp[1] == 0x00:
                stats["win"] += 1
                out.append(f"SOCKS_WIN {addr} code=200")
        except Exception:
            pass
        finally:
            stats["done"] += 1
            if stats["done"] % 2000 == 0:
                print(f"    {stats['done']}/{stats['total']} done, {stats['win']} wins", flush=True)


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
    sample = int(sys.argv[4]) if len(sys.argv) > 4 else 0  # 0 = all

    def load(fn):
        p = os.path.join(HUNT, fn)
        if not os.path.isfile(p):
            return []
        lines = [l.strip() for l in open(p) if IPPORT.match(l.strip())]
        return list(dict.fromkeys(lines))[:sample or None]

    jobs = []
    if mode in ("http", "both"):
        http = load("http_all.txt")
        print(f"[*] HTTP candidates: {len(http)}")
        jobs += [("HTTP", a) for a in http]
    if mode in ("socks", "both"):
        socks = load("socks_all.txt") + load("socks4_all.txt")
        socks = list(dict.fromkeys(socks))[:sample or None] if sample else list(dict.fromkeys(socks))
        print(f"[*] SOCKS candidates: {len(socks)}")
        jobs += [("SOCKS", a) for a in socks]

    print(f"[*] testing {len(jobs)} proxies, {workers} concurrent, {timeout}s timeout...")
    sem = asyncio.Semaphore(workers)
    out, stats = [], {"done": 0, "win": 0, "total": len(jobs)}
    t0 = time.time()
    await asyncio.gather(*(try_http(a, sem, timeout, out, stats) if k == "HTTP"
                           else try_socks(a, sem, timeout, out, stats) for k, a in jobs))
    dt = time.time() - t0

    winners = sorted(out)
    with open(os.path.join(HUNT, "winners_raw.txt"), "w") as f:
        f.write("\n".join(winners) + ("\n" if winners else ""))
    with open(os.path.join(HUNT, "winners.txt"), "w") as f:
        f.write("\n".join(winners) + ("\n" if winners else ""))
    print(f"[*] DONE in {dt:.0f}s ({len(jobs)/dt:.0f}/sec) — winners: {len(winners)}")
    errs = stats.get("errors", {})
    if errs:
        top = ", ".join(f"{k}={v}" for k, v in sorted(errs.items(), key=lambda x: -x[1])[:5])
        print(f"[*] error mix: {top}")
    print(f"[*] saved winners.txt — next: bash proxy_rank.sh (full playability check)")


if __name__ == "__main__":
    # Windows: Proactor (IOCP) loop — no 512-fd select() cap, handles 1000s
    # of concurrent sockets. (SelectorEventLoop caps at 512 and crashes.)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

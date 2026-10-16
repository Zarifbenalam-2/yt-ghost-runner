"""ipshelf/server.py — the LIVE dashboard server for the IP Shelf.

Serves the locked Warehouse Dark UI (ipshelf/ui/index.html) and wires it to
the real core modules (ipshelf.core.shelf / assigner / sweeper / gate) plus
the background-job status files written by the hunt + run_watch agents.

Endpoints
---------
  GET  /              ui/index.html (re-read from disk on every request)
  GET  /api/state     one big JSON the UI polls every 5s
  POST /api/assign    {handle, region, gate?} -> assign_region + draw
  POST /api/regions   {handles, region} -> MASS region lock (LAW 3 forever)
  POST /api/browse    {handle} -> headed browser on the channel's YouTube
                       page through its sticky gold IP (single instance)
  GET  /api/browse    browse status file
  POST /api/sweep     background re-gate of every gold IP (burn + delete)
  GET  /api/sweep     current sweep status file
  POST /api/harvest   spawn  python -m ipshelf.hunt.harvest  (subprocess)
  GET  /api/harvest   harvest status file
  POST /api/run       spawn  python ipshelf/run_watch.py --url ... (subprocess)
  GET  /api/run       run status file (written by run_watch.py)
  GET  /api/log       last raw lines of ipshelf.log + harvest.log, newest last
  anything else       404 JSON

Job-status files record a PID; a 'running' status whose PID is dead (or a
stale file with no PID) is auto-reset so a crashed job can never 409-lock
its button forever. Every endpoint is wrapped: an exception returns
500 {"error": <last traceback line>} and never takes the server thread down.

Run (venv python, from yt-channel-automation or anywhere):
    venv/Scripts/python.exe -m ipshelf.server
    venv/Scripts/python.exe ipshelf/server.py
  -> http://127.0.0.1:8766      (env IPSHELF_PORT / IPSHELF_HOST override;
     env IPSHELF_DATA redirects the data dir — mirrors ipshelf.core.shelf)
"""

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_PKG = Path(__file__).resolve().parent          # .../ipshelf
_YT = _PKG.parent                                # .../yt-channel-automation
if str(_YT) not in sys.path:
    sys.path.insert(0, str(_YT))

# Core import is guarded: parallel agents may still be mid-merge. If it fails
# the server still starts and every core-dependent endpoint returns a clean
# 500 {"error": ...} instead of crashing on boot.
try:
    from ipshelf.core import assigner, gate, shelf, sweeper  # noqa: E402
    _CORE_ERR = None
except Exception:
    assigner = gate = shelf = sweeper = None
    _CORE_ERR = traceback.format_exc().strip().splitlines()[-1]


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


HOST = os.environ.get("IPSHELF_HOST", "127.0.0.1")
PORT = _int_env("IPSHELF_PORT", 8766)
UI_FILE = _PKG / "ui" / "index.html"


# --------------------------------------------------------------------------
# data paths (raw-file endpoints; pool/bindings always go through core shelf)
# --------------------------------------------------------------------------

def _data_dir() -> Path:
    """Mirror of shelf.data_dir(): env IPSHELF_DATA wins, else <ipshelf>/data."""
    env = os.environ.get("IPSHELF_DATA")
    return Path(env) if env else _PKG / "data"


def harvest_status_path() -> Path:
    return _data_dir() / "harvest_status.json"


def sweep_status_path() -> Path:
    return _data_dir() / "sweep_status.json"


def run_status_path() -> Path:
    return _data_dir() / "run_status.json"


def harvest_log_path() -> Path:
    return _data_dir() / "harvest.log"


def run_log_path() -> Path:
    return _data_dir() / "run.log"


def browse_status_path() -> Path:
    return _data_dir() / "browse_status.json"


def megaharvest_status_path() -> Path:
    return _data_dir() / "megaharvest_status.json"


def megaharvest_log_path() -> Path:
    return _data_dir() / "megaharvest.log"


def ipshelf_log_path() -> Path:
    # use the core's own resolver when available (same env handling)
    return shelf.log_path() if shelf is not None else _data_dir() / "ipshelf.log"


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj) -> None:
    """Atomic JSON write (same .tmp + os.replace pattern as core shelf)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _tail_lines(path, n):
    """Last n non-empty lines of a text file ([] when missing)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []
    return lines[-n:]


def _detached_flags() -> int:
    return getattr(subprocess, "DETACHED_PROCESS", 0)


def _pid_alive(pid) -> bool:
    """True when a PID still exists (OpenProcess on Windows, signal 0 on POSIX)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, 0, pid)   # SYNCHRONIZE
        if not h:
            return False
        k.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _fresh_mtime(path, seconds) -> bool:
    """True when the file was modified within the last `seconds`."""
    try:
        age = time.time() - Path(path).stat().st_mtime
    except OSError:
        return False
    return age < seconds


def _lock_path(name: str) -> Path:
    return _data_dir() / (name + ".lock")


def _lock_alive(name: str) -> bool:
    """Is the single-instance job (browse / create) still running?"""
    p = _lock_path(name)
    d = _read_json(p)
    if not d:
        return False
    if _pid_alive(d.get("pid")):
        return True
    try:
        p.unlink()          # stale lock from a dead process — remove it
    except OSError:
        pass
    return False


def _write_lock(name: str, pid) -> None:
    _data_dir().mkdir(parents=True, exist_ok=True)
    _write_json(_lock_path(name), {"pid": pid, "at": shelf.now_ts()
                                   if shelf is not None else ""})


def _job_view(path, max_age_sec):
    """Read a job status file and flag dead/stale 'running' states.

    A status file left at running:true by a process that died (or by a server
    restart) must not 409-lock its button forever:
      - pid recorded + dead       -> running flipped to False, note set
      - no pid + file old         -> same (mtime fallback)
    """
    st = _read_json(path)
    if not isinstance(st, dict) or not st.get("running"):
        return st
    pid = st.get("pid")
    if pid is not None and not _pid_alive(pid):
        st = dict(st)
        st["running"] = False
        st["stale"] = True
        st["note"] = "process died without finishing (status auto-reset)"
    elif pid is None and not _fresh_mtime(path, max_age_sec):
        st = dict(st)
        st["running"] = False
        st["stale"] = True
        st["note"] = "old running status without a pid (auto-reset)"
    return st


# --------------------------------------------------------------------------
# /api/state
# --------------------------------------------------------------------------

def _latest_sweep_report():
    """Latest per-sweep report written by sweeper into data/sweeps/, or None."""
    d = _data_dir() / "sweeps"
    try:
        files = sorted(p for p in d.glob("*.json") if p.is_file())
    except OSError:
        return None
    return _read_json(files[-1]) if files else None


def megaharvest_view():
    """Megaharvest orchestrator state with liveness detection.

    pid from the status file when alive; a 'running' status with a dead pid
    (or no pid and a 2h-stale file — a round cycle is ~60 min) is flipped to
    'dead' so the START button can never be locked by a ghost.
    """
    st = _read_json(megaharvest_status_path()) or {}
    if not isinstance(st, dict) or not st:
        return {"state": "idle", "running": False}
    st = dict(st)
    state = str(st.get("state") or "idle")
    if state in ("running", "sweeping"):
        alive = _pid_alive(st.get("pid")) if st.get("pid") else False
        if not alive and not _fresh_mtime(megaharvest_status_path(), 7200):
            st["state"] = "dead"
            st["note"] = "orchestrator gone (reboot or crash) — auto-detected"
            st["running"] = False
        else:
            st["running"] = True
    else:
        st["running"] = False
    return st


def build_state() -> dict:
    """The single big JSON the UI polls. All shelf logic goes through core."""
    if shelf is None:
        raise RuntimeError(f"ipshelf.core unavailable: {_CORE_ERR}")

    pool = shelf.load_pool()
    bindings = shelf.load_bindings()
    shelves = shelf.shelf_regions(pool)

    exits = pool.get("exits", []) if isinstance(pool, dict) else []
    golds = [e for e in exits if e.get("status") == "gold"]
    free_ips_total = sum(1 for e in golds
                         if len(e.get("channels", [])) < shelf.CAP)
    bound_channels = sum(
        1 for b in bindings.values()
        if isinstance(b, dict) and b.get("sticky_ip"))
    regions_low = [r.get("region") for r in shelves if r.get("needs_attention")]

    # last 100 PARSED json lines from ipshelf.log (raw lines skipped)
    log_tail = []
    for ln in _tail_lines(ipshelf_log_path(), 200):
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            log_tail.append(obj)
    log_tail = log_tail[-100:]

    # live channel inventory from the shared DB (personal channels filtered
    # out — they are never automated)
    channels = []
    db_error = None
    try:
        import engine.db as db
        PERSONAL = {"@ZarifBenAlam"}
        for ch in db.conn().execute(
                "SELECT * FROM Channel ORDER BY createdAt").fetchall():
            d = dict(ch)
            if d.get("handle") in PERSONAL or d.get("status") == "personal":
                d["_personal"] = True
            channels.append(d)
    except Exception:
        db_error = traceback.format_exc().strip().splitlines()[-1]
    bindings_map = bindings if isinstance(bindings, dict) else {}
    bound_set = {c.get("handle") for c in channels if c.get("handle")}
    extra_handles = [h for h in bindings_map if h not in bound_set]

    return {
        "pool": {"updated": pool.get("updated"), "exits": exits},
        "bindings": bindings_map,
        "shelves": shelves,
        "channels": channels,
        "channels_extra": extra_handles,
        "db_error": db_error,
        "stats": {
            "total_gold": len(golds),
            "free_total": free_ips_total,
            "bound_channels": bound_channels,
            "cap_per_ip": shelf.CAP,
            "regions_low": regions_low,
        },
        "harvest": _job_view(harvest_status_path(), max_age_sec=7200),
        "sweep": _job_view(sweep_status_path(), max_age_sec=7200),
        "sweeps": _latest_sweep_report(),
        "harvest_tail": _tail_lines(harvest_log_path(), 40),
        "megaharvest": megaharvest_view(),
        "megaharvest_tail": _tail_lines(megaharvest_log_path(), 30),
        "run": _job_view(run_status_path(), max_age_sec=7200),
        "browse": _read_json(browse_status_path()),
        "log_tail": log_tail,
        "server_time": shelf.now_ts(),
    }


# --------------------------------------------------------------------------
# POST /api/assign
# --------------------------------------------------------------------------

def do_assign(body: dict):
    """assign_region(handle, region) then draw_for_channel(region=region).

    body.gate=true passes the REAL quick gold gate into the draw (can take
    ~18s per candidate); default gate=false trusts the stored gold status —
    the sweeper re-gates everything for real.
    Returns (http_code, payload).
    """
    if assigner is None or gate is None:
        return 503, {"error": f"ipshelf.core unavailable: {_CORE_ERR}"}
    handle = str(body.get("handle") or "").strip()
    region = str(body.get("region") or "").strip()
    if not handle or not region:
        return 400, {"error": "bad_body",
                     "hint": "needs {handle: '@x', region: 'US|GB|DE|T1|cc'}"}

    lock = assigner.assign_region(handle, region)
    if lock.get("error"):
        return 200, {"ok": False, "step": "assign_region", "result": lock}

    gate_fn = gate.quick_gold_check if body.get("gate") else None
    result = assigner.draw_for_channel(handle, region=region, gate_fn=gate_fn)
    return 200, {"ok": not result.get("error"), "result": result}


# --------------------------------------------------------------------------
# POST /api/sweep  (background thread; real network checks, minutes-long)
# --------------------------------------------------------------------------

class _SweepCtl:
    lock = threading.Lock()
    thread = None


def _sweep_bg(started_at: str) -> None:
    try:
        report = sweeper.sweep()
        status = {"running": False, "started_at": started_at,
                  "finished_at": shelf.now_ts()}
        status.update(report)
    except Exception:
        status = {"running": False, "started_at": started_at,
                  "finished_at": shelf.now_ts(),
                  "error": traceback.format_exc().strip().splitlines()[-1]}
    _write_json(sweep_status_path(), status)


def start_sweep():
    """Start the burn pass in a background thread. 409 when already running."""
    if sweeper is None or shelf is None:
        return 503, {"error": f"ipshelf.core unavailable: {_CORE_ERR}"}
    with _SweepCtl.lock:
        if _SweepCtl.thread is not None and _SweepCtl.thread.is_alive():
            return 409, {"error": "already running"}
        st = _read_json(sweep_status_path()) or {}
        if st.get("running"):
            return 409, {"error": "already running", "sweep": st}
        started = shelf.now_ts()
        _write_json(sweep_status_path(), {"running": True, "started_at": started})
        _SweepCtl.thread = threading.Thread(
            target=_sweep_bg, args=(started,), daemon=True,
            name="ipshelf-sweep")
        _SweepCtl.thread.start()
    return 200, {"ok": True, "started_at": started}


# --------------------------------------------------------------------------
# POST /api/harvest  (subprocess: python -m ipshelf.hunt.harvest)
# --------------------------------------------------------------------------

def harvest_module_ready() -> bool:
    return (_PKG / "hunt" / "harvest.py").is_file()


def start_harvest():
    hs = _job_view(harvest_status_path(), max_age_sec=7200) or {}
    if hs.get("running") and _pid_alive(hs.get("pid")):
        return 409, {"error": "already running", "harvest": hs,
                     "pid": hs.get("pid")}
    if not harvest_module_ready():
        return 503, {"error": "ipshelf/hunt/harvest.py not present "
                             "(parallel module pending)"}
    _data_dir().mkdir(parents=True, exist_ok=True)
    logf = open(harvest_log_path(), "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ipshelf.hunt.harvest"],
            cwd=str(_YT), stdout=logf, stderr=subprocess.STDOUT,
            creationflags=_detached_flags())
    finally:
        logf.close()
    # server-side status stamp with the PID so a dead harvest can never
    # 409-lock the button forever (harvest.py overwrites the file itself)
    _write_json(harvest_status_path(), {
        "running": True, "pid": proc.pid, "started_at": shelf.now_ts(),
        "note": "hunt machine spawned by dashboard"})
    return 200, {"ok": True, "pid": proc.pid}


# --------------------------------------------------------------------------
# POST /api/megaharvest/start|stop  (detached: ipshelf/megaharvest.py loop)
# --------------------------------------------------------------------------

def megaharvest_ready() -> bool:
    return (_PKG / "megaharvest.py").is_file()


def start_megaharvest(body: dict):
    st = megaharvest_view()
    if st.get("running"):
        return 409, {"error": "already running", "megaharvest": st,
                     "pid": st.get("pid")}
    if not megaharvest_ready():
        return 503, {"error": "ipshelf/megaharvest.py not present"}
    try:
        target = int(body.get("target") or 200)
        deadline = float(body.get("deadline") or 6.0)
    except (TypeError, ValueError):
        return 400, {"error": "bad_target_deadline"}
    _data_dir().mkdir(parents=True, exist_ok=True)
    outf = open(_data_dir() / "mh_out.log", "ab")
    errf = open(_data_dir() / "mh_err.log", "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(_PKG / "megaharvest.py"),
             "--target", str(target), "--deadline", str(deadline)],
            cwd=str(_YT), stdout=outf, stderr=errf,
            creationflags=_detached_flags())
    finally:
        outf.close()
        errf.close()
    _write_json(megaharvest_status_path(), {
        "state": "running", "pid": proc.pid, "ts": shelf.now_ts(),
        "note": "spawned by dashboard (status rewritten by orchestrator)"})
    return 200, {"ok": True, "pid": proc.pid, "target": target,
                 "deadline_h": deadline}


def stop_megaharvest():
    st = _read_json(megaharvest_status_path()) or {}
    pid = st.get("pid")
    if not pid or not _pid_alive(pid):
        return 404, {"error": "not running"}
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True)
    else:
        import signal as _sig
        os.kill(int(pid), _sig.SIGTERM)
    _write_json(megaharvest_status_path(), {
        "state": "stopped_by_user", "ts": shelf.now_ts()})
    return 200, {"ok": True, "stopped": pid}


# --------------------------------------------------------------------------
# POST /api/run  (subprocess: python ipshelf/run_watch.py --url ...)
# --------------------------------------------------------------------------

def run_watch_ready() -> bool:
    return (_PKG / "run_watch.py").is_file()


def start_run(body: dict):
    url = str(body.get("url") or "").strip()
    if not url:
        return 400, {"error": "bad_url",
                     "hint": "body needs a YouTube watch url"}

    rs = _job_view(run_status_path(), max_age_sec=7200) or {}
    if rs.get("running") and _pid_alive(rs.get("pid")):
        return 409, {"error": "already running", "run": rs}
    if not run_watch_ready():
        return 503, {"error": "ipshelf/run_watch.py not present "
                             "(parallel module pending)"}

    handles = body.get("handles") or []
    if isinstance(handles, str):
        handles = handles.split(",")
    handles = [str(h).strip() for h in handles if str(h).strip()]
    try:
        count = int(body.get("count") or 10)
    except (TypeError, ValueError):
        count = 10
    region = str(body.get("region") or "").strip()
    auto = bool(body.get("auto_harvest"))

    cmd = [sys.executable, "ipshelf/run_watch.py", "--url", url]
    if handles:
        cmd += ["--handles", ",".join(handles)]
    cmd += ["--count", str(count)]
    if region:
        cmd += ["--region", region]
    if auto:
        cmd += ["--auto_harvest"]

    _data_dir().mkdir(parents=True, exist_ok=True)
    logf = open(run_log_path(), "ab")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(_YT), stdout=logf, stderr=subprocess.STDOUT,
            creationflags=_detached_flags())
    finally:
        logf.close()
    _write_json(run_status_path(), {
        "running": True, "pid": proc.pid, "started_at": shelf.now_ts(),
        "url": url, "count": count, "region": region,
        "auto_harvest": auto, "log": [],
        "note": "watch run spawned by dashboard"})
    return 200, {"ok": True, "pid": proc.pid, "cmd": cmd}


# --------------------------------------------------------------------------
# POST /api/regions  (mass region lock for many channels at once)
# --------------------------------------------------------------------------

def do_regions(body: dict):
    """assign_region() for a list of handles. Returns per-handle results.

    body: {handles: ["@a","@b"], region: "US"} — region locks are FOREVER
    (LAW 3); already-locked channels with a different region are refused
    per-handle (not an error for the batch).
    """
    if assigner is None:
        return 503, {"error": f"ipshelf.core unavailable: {_CORE_ERR}"}
    handles = body.get("handles") or []
    if isinstance(handles, str):
        handles = handles.split(",")
    handles = [str(h).strip() for h in handles if str(h).strip()]
    region = str(body.get("region") or "").strip()
    if not handles or not region:
        return 400, {"error": "bad_body",
                     "hint": "needs {handles: ['@a',...], region: 'US|T1|cc'}"}
    results = {}
    for h in handles:
        results[h] = assigner.assign_region(h, region)
    ok_n = sum(1 for r in results.values() if not r.get("error"))
    shelf.append_log("mass_assign_region", region=region,
                     handles=handles, ok=ok_n)
    return 200, {"ok": True, "region": region, "locked": ok_n,
                 "total": len(handles), "results": results}


# --------------------------------------------------------------------------
# POST /api/browse  (open a channel's YouTube page in a HEADED browser
# through its sticky gold IP — single-instance lock)
# --------------------------------------------------------------------------

def browse_ready() -> bool:
    return (_PKG / "prove_binding.py").is_file()


def start_browse(body: dict):
    """Spawn `prove_binding.py --hold --url <channel url>` headed.

    One browse window at a time (single browser profile = single-user lock).
    The browser stays open until the user closes it; the spawned process
    keeps browse_status.json updated (proof verdict on exit).
    """
    handle = str(body.get("handle") or "").strip()
    if not handle:
        return 400, {"error": "bad_body", "hint": "needs {handle: '@x'}"}
    if _lock_alive("browse"):
        return 409, {"error": "browse window already open — close it first"}
    if not browse_ready():
        return 503, {"error": "ipshelf/prove_binding.py missing"}

    # the channel's own URL from the DB when available; else handle -> /@
    url = str(body.get("url") or "").strip()
    if not url:
        try:
            import engine.db as db
            ch = db.conn().execute(
                "SELECT url, handle FROM Channel WHERE handle=? LIMIT 1",
                (handle,)).fetchone()
            if ch and ch["url"]:
                url = ch["url"]
        except Exception:
            pass
    if not url:
        url = "https://www.youtube.com/" + handle

    cmd = [sys.executable, str(_PKG / "prove_binding.py"),
           "--handle", handle, "--hold", "--url", url]
    _data_dir().mkdir(parents=True, exist_ok=True)
    logf = open(_data_dir() / "browse.log", "ab")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(_YT), stdout=logf, stderr=subprocess.STDOUT,
            creationflags=_detached_flags())
    finally:
        logf.close()
    _write_lock("browse", proc.pid)
    _write_json(browse_status_path(), {
        "running": True, "pid": proc.pid, "handle": handle, "url": url,
        "started_at": (shelf.now_ts() if shelf is not None else "")})
    return 200, {"ok": True, "pid": proc.pid, "handle": handle, "url": url}


# --------------------------------------------------------------------------
# GET /api/log
# --------------------------------------------------------------------------

def api_log() -> dict:
    """Last 200 raw lines of ipshelf.log + harvest.log combined, newest last.

    JSON lines get their "ts" as sort key so the two files interleave in
    true chronological order; non-JSON lines sort to the front.
    """
    entries = []
    for ln in _tail_lines(ipshelf_log_path(), 200) + \
              _tail_lines(harvest_log_path(), 200):
        ts = ""
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                ts = str(obj.get("ts") or "")
        except ValueError:
            pass
        entries.append((ts, ln))
    entries.sort(key=lambda e: e[0])
    return {"lines": [ln for _, ln in entries][-200:]}


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "IPShelf/1.0"

    def log_message(self, fmt, *args):      # quiet default request logging
        pass

    # ---- plumbing ----------------------------------------------------

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError, OSError):
            pass  # client hung up mid-response; never crash the thread

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_body(self):
        """Parse the request body as a JSON object; None when unparseable."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else {}

    def _guard(self, fn):
        """Run an endpoint; any exception -> 500 {error: last tb line}."""
        try:
            fn()
        except Exception:
            last = traceback.format_exc().strip().splitlines()[-1][:400]
            try:
                self._json(500, {"error": last})
            except Exception:
                pass

    # ---- routing -----------------------------------------------------

    def do_GET(self):
        self._guard(self._route_get)

    def do_POST(self):
        self._guard(self._route_post)

    def _route_get(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                html = UI_FILE.read_text(encoding="utf-8")
            except OSError:
                self._json(500, {"error": f"ui file missing: {UI_FILE}"})
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(200, build_state())
        elif path == "/api/sweep":
            self._json(200, _read_json(sweep_status_path())
                       or {"running": False, "never": True})
        elif path == "/api/harvest":
            self._json(200, _read_json(harvest_status_path())
                       or {"running": False, "never": True})
        elif path == "/api/megaharvest":
            self._json(200, megaharvest_view())
        elif path == "/api/run":
            self._json(200, _job_view(run_status_path(), max_age_sec=7200)
                       or {"running": False, "never": True})
        elif path == "/api/browse":
            st = _job_view(browse_status_path(), max_age_sec=86400)
            if st is None:
                st = _read_json(browse_status_path()) \
                     or {"running": False, "never": True}
            self._json(200, st)
        elif path == "/api/log":
            self._json(200, api_log())
        else:
            self._json(404, {"error": "not_found", "path": path})

    def _route_post(self):
        path = self.path.split("?")[0]
        body = self._read_body()
        if body is None:
            self._json(400, {"error": "bad_json_body"})
            return
        if path == "/api/assign":
            code, payload = do_assign(body)
        elif path == "/api/regions":
            code, payload = do_regions(body)
        elif path == "/api/browse":
            code, payload = start_browse(body)
        elif path == "/api/sweep":
            code, payload = start_sweep()
        elif path == "/api/harvest":
            code, payload = start_harvest()
        elif path == "/api/megaharvest/start":
            code, payload = start_megaharvest(body)
        elif path == "/api/megaharvest/stop":
            code, payload = stop_megaharvest()
        elif path == "/api/run":
            code, payload = start_run(body)
        else:
            code, payload = 404, {"error": "not_found", "path": path}
        self._json(code, payload)


def main():
    class _Server(ThreadingHTTPServer):
        daemon_threads = True

    srv = _Server((HOST, PORT), Handler)
    print("=" * 64)
    print("IP SHELF — live dashboard (Warehouse Dark)")
    print(f"  URL      : http://{HOST}:{PORT}")
    print(f"  UI file  : {UI_FILE}")
    print(f"  data dir : {_data_dir()}")
    if _CORE_ERR:
        print(f"  WARNING  : ipshelf.core import FAILED — core endpoints "
              f"will error: {_CORE_ERR}")
    else:
        print("  core     : ipshelf.core OK (shelf / assigner / sweeper / gate)")
    print("  stop     : Ctrl+C")
    print("=" * 64)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[ipshelf/server] stopped.")


if __name__ == "__main__":
    main()

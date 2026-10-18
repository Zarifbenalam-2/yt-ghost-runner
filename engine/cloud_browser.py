"""cloud_browser.py — Cross-platform browser launcher for Ghost Watcher.

Works seamlessly on:
1. Linux GitHub Actions (ubuntu-latest with Xvfb / headless)
2. Local Windows (CloakBrowser Chromium binary)
3. Standard Playwright Chromium fallback

Persona integration (fingerprints.py): each tab gets its own coherent
identity, applied here via (a) persona -> Playwright context kwargs for the
signals Playwright owns natively, (b) an init-script for the JS-realm
surfaces (webdriver, client-hints, GPU, canvas, cores), and (c) when using
the patched CloakBrowser binary, `--fingerprint-*` flags so the C++ layer
covers the surface again in a mutually-consistent way.
"""
import glob
import os
import sys
from pathlib import Path

try:
    from .fingerprints import cloak_args
except Exception:
    cloak_args = lambda persona: []   # noqa: E731 — import guard for odd CWDs

DEFAULT_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
]


def detect_chromium_executable():
    """Find CloakBrowser binary (Windows .exe or Linux chrome), fallback to None for Playwright default."""
    env_bin = os.environ.get("BROWSER_EXECUTABLE_PATH")
    if env_bin and Path(env_bin).exists():
        return Path(env_bin)

    home = Path(os.path.expanduser("~")) / ".cloakbrowser"

    if sys.platform == "win32":
        pinned = home / "chromium-146.0.7680.177.5" / "chrome.exe"
        if pinned.exists():
            return pinned
        candidates = sorted(glob.glob(str(home / "chromium-*" / "chrome.exe")))
        if candidates:
            return Path(candidates[-1])
    else:
        # Linux: CloakBrowser installs to ~/.cloakbrowser/chromium-<ver>/chrome
        pinned = home / "chromium-146.0.7680.177.5" / "chrome"
        if pinned.exists():
            return pinned
        candidates = sorted(glob.glob(str(home / "chromium-*" / "chrome")))
        if candidates:
            return Path(candidates[-1])

    return None


def open_ghost_context(p, proxy_server=None, user_data_dir=None, headless=True,
                       persona=None):
    """Launch an ephemeral, isolated browser context for ghost watching.

    persona: engine.fingerprints.Persona for this tab+IP, or None for the
    legacy uniform identity. When present, its signals are applied through
    three layers (context kwargs + init script + CloakBrowser flags) so all
    fingerprint surfaces move together and stay consistent with the proxy geo.
    """
    from playwright.sync_api import Error as PwError

    exe = detect_chromium_executable()
    args = list(DEFAULT_ARGS)
    if persona is not None:
        args += cloak_args(persona)
    else:
        args += ["--window-size=1280,720"]

    if proxy_server:
        args.append(f"--proxy-server={proxy_server}")

    launch_kwargs = {
        "headless": headless,
        "args": args,
    }

    if exe:
        launch_kwargs["executable_path"] = str(exe)

    ctx = None
    if user_data_dir:
        launch_kwargs["user_data_dir"] = str(user_data_dir)
        # persona-driven context options; empty dict keeps old behavior
        if persona is not None:
            launch_kwargs.update(persona.to_context())
        try:
            ctx = p.chromium.launch_persistent_context(**launch_kwargs)
        except PwError:
            # a bad persona value (e.g. an exotic timezone id) must never kill
            # the tab — retry with the persona's signals stripped
            launch_kwargs.pop("user_data_dir", None)
            launch_kwargs.pop("viewport", None)
            launch_kwargs.pop("screen", None)
            launch_kwargs.pop("device_scale_factor", None)
            launch_kwargs.pop("is_mobile", None)
            launch_kwargs.pop("has_touch", None)
            launch_kwargs.pop("locale", None)
            launch_kwargs.pop("timezone_id", None)
            launch_kwargs.pop("user_agent", None)
            launch_kwargs["user_data_dir"] = str(user_data_dir)
            ctx = p.chromium.launch_persistent_context(**launch_kwargs)
    else:
        browser = p.chromium.launch(**launch_kwargs)
        ckw = {}
        if persona is not None:
            ckw = persona.to_context()
        else:
            ckw = {
                "viewport": {"width": 1280, "height": 720},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            }
        ctx = browser.new_context(**ckw)

    if persona is not None:
        try:
            ctx.add_init_script(persona.init_script())
        except Exception:
            pass

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(45000)
    return ctx, page

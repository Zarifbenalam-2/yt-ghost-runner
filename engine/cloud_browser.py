"""cloud_browser.py — Cross-platform browser launcher for Ghost Watcher.

Works seamlessly on:
1. Linux GitHub Actions (ubuntu-latest with Xvfb / headless)
2. Local Windows (CloakBrowser Chromium binary)
3. Standard Playwright Chromium fallback
"""
import glob
import os
import sys
from pathlib import Path

DEFAULT_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--window-size=1280,720",
]


def detect_chromium_executable():
    """Find CloakBrowser binary on Windows, or return None to use Playwright bundled Chromium."""
    env_bin = os.environ.get("BROWSER_EXECUTABLE_PATH")
    if env_bin and Path(env_bin).exists():
        return Path(env_bin)

    if sys.platform == "win32":
        home = Path(os.path.expanduser("~")) / ".cloakbrowser"
        pinned = home / "chromium-146.0.7680.177.5" / "chrome.exe"
        if pinned.exists():
            return pinned
        candidates = sorted(glob.glob(str(home / "chromium-*" / "chrome.exe")))
        if candidates:
            return Path(candidates[-1])

    # On Linux CI or standard environments, return None to use Playwright default Chromium
    return None


def open_ghost_context(p, proxy_server=None, user_data_dir=None, headless=True):
    """Launch an ephemeral, isolated browser context for ghost watching."""
    exe = detect_chromium_executable()
    args = list(DEFAULT_ARGS)
    
    if proxy_server:
        args.append(f"--proxy-server={proxy_server}")

    launch_kwargs = {
        "headless": headless,
        "args": args,
    }

    if exe:
        launch_kwargs["executable_path"] = str(exe)

    if user_data_dir:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            **launch_kwargs
        )
    else:
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(45000)
    return ctx, page

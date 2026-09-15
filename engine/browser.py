"""Account-aware browser launching, generalizing hunt_lib/wu_lib.

Every function takes an account_id; 'default' maps to the original
cloak-profile/ so all existing scripts keep working unchanged.
"""
import glob
import os
from pathlib import Path

from engine.config import account_profile_dir
from hunt_lib import ROOT  # noqa: F401  (keep single source of paths)

DEFAULT_ARGS = ["--disable-blink-features=AutomationControlled"]


def cloak_exe():
    """CloakBrowser binary with version-detection fallback (pinned first, then newest)."""
    home = Path(os.path.expanduser("~")) / ".cloakbrowser"
    pinned = home / "chromium-146.0.7680.177.5" / "chrome.exe"
    if pinned.exists():
        return pinned
    candidates = sorted(glob.glob(str(home / "chromium-*" / "chrome.exe")))
    if candidates:
        return Path(candidates[-1])
    raise RuntimeError(f"No CloakBrowser chromium found under {home}")


def open_context(p, headless=True, account_id="default"):
    """Persistent context on this account's profile (no proxy)."""
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(account_profile_dir(account_id)),
        executable_path=str(cloak_exe()),
        headless=headless,
        args=DEFAULT_ARGS if not headless else [],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(45000)
    return ctx, page


def open_proxied_arg(p, proxy_server, headless=True, account_id="default"):
    """Persistent context routed via Chromium --proxy-server (socks4/5/http).

    Use this for socks4 free proxies and Tor (socks5://127.0.0.1:<port>).
    """
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(account_profile_dir(account_id)),
        executable_path=str(cloak_exe()),
        headless=headless,
        args=[f"--proxy-server={proxy_server}", *DEFAULT_ARGS],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(45000)
    return ctx, page


def open_visible(p, account_id="default", maximized=True):
    """Visible browser for manual steps (login, phone verification)."""
    args = ["--start-maximized"] if maximized else []
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(account_profile_dir(account_id)),
        executable_path=str(cloak_exe()),
        headless=False,
        args=args,
        no_viewport=True,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page

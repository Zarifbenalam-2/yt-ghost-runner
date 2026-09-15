"""Shared helpers for hunt steps: open the saved Cloak browser profile, capture evidence."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HUNT = ROOT / "_hunt"
PROFILE = ROOT / "cloak-profile"
ANCHORS = ROOT / "anchor_points.json"
RECORDING = ROOT / "create_channel.recording.json"

# CloakBrowser patched Chromium binary (already local, no download needed).
CLOAK_EXE = Path(os.path.expanduser("~")) / ".cloakbrowser" / "chromium-146.0.7680.177.5" / "chrome.exe"


def open_context(p, headless=True):
    """Open Cloak's patched Chromium reusing the persistent profile saved by login.py."""
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        executable_path=str(CLOAK_EXE),
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"] if not headless else [],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def capture(page, name, action, note=""):
    """Save screenshot + full DOM + JSON summary for one hunt step."""
    HUNT.mkdir(exist_ok=True)
    png = HUNT / f"{name}.png"
    html = HUNT / f"{name}.html"
    meta = HUNT / f"{name}.json"
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception:
        page.screenshot(path=str(png))
    html.write_text(page.content(), encoding="utf-8")
    try:
        body = page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 2000) : ''"
        )
    except Exception:
        body = ""
    meta.write_text(
        json.dumps(
            {
                "step": name,
                "action": action,
                "note": note,
                "url": page.url,
                "title": page.title(),
                "body_snippet": body,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"[captured] {png.name} | {html.name} | url={page.url}")


def load_anchors():
    return json.loads(ANCHORS.read_text(encoding="utf-8"))


def save_anchor(phase, key, selector, note=""):
    data = load_anchors()
    data.setdefault(phase, {})[key] = {"selector": selector, "note": note}
    ANCHORS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[anchor] {phase}.{key} = {selector}")


def record_step(action, selector, note=""):
    data = json.loads(RECORDING.read_text(encoding="utf-8"))
    data["steps"].append({"action": action, "selector": selector, "note": note})
    RECORDING.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[recorded] {action} {selector}")

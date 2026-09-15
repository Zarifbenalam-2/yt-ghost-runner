"""Engine configuration: loads YAML files from config/ and exposes typed access.

All previously hardcoded values (seed videos, handles, pacing, caps) live here.
"""
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # graceful fallback: YAML configs won't load, but non-YAML code works

ENGINE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ENGINE_ROOT.parent          # yt-ghost-runner/
APP_ROOT = PROJECT_ROOT.parent             # streamstress-full-project/
CONFIG_DIR = PROJECT_ROOT / "config"
SHARED_DB = APP_ROOT / "prisma" / "db" / "custom.db"
PROFILES_DIR = PROJECT_ROOT / "profiles"

_cache = {}


def _load(name):
    if name in _cache:
        return _cache[name]
    path = CONFIG_DIR / name
    if not path.exists():
        _cache[name] = {}
        return _cache[name]
    if yaml is None:
        _cache[name] = {}
        return _cache[name]
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _cache[name] = data
    return data


def accounts_cfg():
    return _load("accounts.yaml")


def niches_cfg():
    return _load("niches.yaml")


def comments_cfg():
    return _load("comments.yaml")


def policy_cfg():
    return _load("policy.yaml")


def reload():
    _cache.clear()


def account_by_email(email):
    """Return the account dict for an email, or None."""
    for acc in accounts_cfg().get("accounts", []):
        if acc.get("email", "").lower() == email.lower():
            return acc
    return None


def account_profile_dir(account_id):
    """Profile dir for an account id; 'default' keeps the original cloak-profile."""
    if account_id == "default":
        return PROJECT_ROOT / "cloak-profile"
    return PROFILES_DIR / account_id


def new_id(prefix):
    """Prisma-compatible id (cuid-like) for raw SQL inserts."""
    import time
    import uuid
    return f"{prefix}{int(time.time()*1000):x}{uuid.uuid4().hex[:16]}"

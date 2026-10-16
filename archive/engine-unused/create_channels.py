"""Minimal stub: only the _acct_key helper needed by engine.warmup."""


def _acct_key(acc):
    """profile dir name from profileDir path ('default' -> legacy profile)."""
    pd = acc["profileDir"] or ""
    if pd in ("cloak-profile", ""):
        return "default"
    return pd.split("/")[-1].split("\\")[-1]

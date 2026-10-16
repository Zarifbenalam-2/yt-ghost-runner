"""gh.py — GitHub API access with retry, backoff and rate-limit awareness.

Every GitHub call in the project (dispatch, artifact pull, log fetch,
watch-detection) goes through here. Before this module each call site had
its own urllib.request with no retry — and during the build we hit real
transient 500s and 403 rate limits that silently degraded behavior.

Design:
  - retries on 5xx, 429 and 403-rate-limit, with exponential backoff + jitter
  - honours Retry-After when GitHub sends it
  - distinguishes "auth broken" (fail fast, don't retry) from "transient"
  - returns parsed JSON (or raw text for logs/artifacts)
"""
import json
import random
import time
import urllib.error
import urllib.request

from .config import GH_BACKOFF, GH_RETRIES, GH_TIMEOUT
from .logging_setup import get_logger

log = get_logger("hub.gh")

API = "https://api.github.com"

#: statuses worth retrying
_RETRY_STATUS = {429, 500, 502, 503, 504}


class GitHubError(RuntimeError):
    """Raised when a GitHub call fails after all retries."""
    def __init__(self, message, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


def _headers(token, accept="application/vnd.github+json"):
    h = {"Accept": accept, "User-Agent": "ghost-hub"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def request(method, url, token="", data=None, accept="application/vnd.github+json",
            timeout=None, raw=False, retries=None):
    """Perform a GitHub API call with retry/backoff.

    Args:
      method   HTTP verb.
      url      absolute URL (or a path starting with '/' -> api.github.com).
      token    bearer token (optional; raises clarity for private repos).
      data     dict -> JSON body.
      raw      return decoded text instead of parsed JSON (for log endpoints).
      retries  override the default retry count.

    Raises GitHubError on non-retryable failure or exhausted retries.
    """
    if url.startswith("/"):
        url = API + url
    timeout = timeout or GH_TIMEOUT
    attempts = (GH_RETRIES if retries is None else retries) + 1
    body = json.dumps(data).encode() if data is not None else None
    last_err = None

    for attempt in range(attempts):
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=_headers(token, accept))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8", "replace")
                return payload if raw else (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as e:
            status = e.code
            text = ""
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            # auth/permission problems are not transient — fail fast
            if status in (401, 404):
                raise GitHubError(f"HTTP {status} {url}", status, text) from e
            if status == 403 and "rate limit" not in text.lower() \
                    and "secondary rate" not in text.lower():
                raise GitHubError(f"HTTP 403 {url}", status, text) from e
            last_err = GitHubError(f"HTTP {status} {url}", status, text)
            retry_after = e.headers.get("Retry-After") if e.headers else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() \
                else GH_BACKOFF ** attempt + random.uniform(0, 0.5)
            log.warning("github %s %s -> HTTP %s (attempt %d/%d), retry in %.1fs",
                        method, url.split("github.com")[-1], status, attempt + 1,
                        attempts, delay)
            if attempt == attempts - 1:
                break
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = GitHubError(f"network error {url}: {e}")
            delay = GH_BACKOFF ** attempt + random.uniform(0, 0.5)
            log.warning("github %s %s -> %s (attempt %d/%d), retry in %.1fs",
                        method, url.split("github.com")[-1], type(e).__name__,
                        attempt + 1, attempts, delay)
            if attempt == attempts - 1:
                break
            time.sleep(delay)

    log.error("github %s %s failed after %d attempts", method, url, attempts)
    raise last_err or GitHubError(f"failed {url}")


# ------------------------------------------------------------ convenience

def get(path, token="", **kw):
    return request("GET", path, token=token, **kw)


def post(path, token="", data=None, **kw):
    return request("POST", path, token=token, data=data or {}, **kw)


def checks_ok(path, token="", timeout=8):
    """Cheap reachability probe: True on 2xx, False on any error (no raise)."""
    try:
        request("GET", path, token=token, timeout=timeout, retries=0)
        return True
    except GitHubError:
        return False


def watch_runs_active(token="", repo=None, per_page=5):
    """Return True iff a watcher run is queued/in_progress.

    Returns None when the check could not be made (so callers can decide;
    the factory previously 'failed open' silently on a 403, which let it
    harvest forever with no watch running).
    """
    from .config import REPO, WORKFLOW_WATCH
    repo = repo or REPO
    try:
        data = get(f"/repos/{repo}/actions/workflows/{WORKFLOW_WATCH}/runs"
                   f"?per_page={per_page}", token=token)
    except GitHubError as e:
        log.warning("watch-check failed: %s", e)
        return None
    for run in data.get("workflow_runs", []):
        if run.get("status") in ("in_progress", "queued"):
            return True
    return False
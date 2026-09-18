# Ghost Runner Scale Architecture

## 1. Proven facts (from CI runs #1-#9)

| Fact | Evidence |
|------|----------|
| Runner spec (public repo, `ubuntu-latest`) | 4 vCPU / 16 GB RAM / 14 GB SSD ([docs](https://docs.github.com/actions/reference/runners/github-hosted-runners)) |
| 2 tabs/runner | PASS (runs #3, #4, #5) |
| 5 tabs/runner | PASS (runs #6, #7) |
| 8 tabs/runner | PASS (run #8) |
| 10 tabs/runner | TESTING (run #9, autopilot) |
| CloakBrowser stealth Chromium on CI | PASS (run #5+) via `pip install cloakbrowser` |
| Artifact exfil (report + screenshots) | PASS (run #7: 4/4 artifacts, 266-431 KB each) |
| Free plan concurrency | 20 jobs account-wide ([limits](https://docs.github.com/actions/reference/limits)) |
| Matrix cap | 256 jobs/run (excess queues) |
| Job timeout | 6 h max (workflow sets 360 min) |

## 2. Target topology

```
                    ┌─────────────────────────────────┐
                    │        DASHBOARD (your PC)      │
                    │  server.py :8766                │
                    │  ┌──────────┐  ┌─────────────┐  │
                    │  │ Live grid│  │ CI reports  │  │
                    │  │ (tunnel) │  │ (artifacts) │  │
                    │  └──────────┘  └─────────────┘  │
                    │  accounts.json (PAT per acct)   │
                    └────────┬────────────────┬───────┘
              dispatch (PAT) │                │ poll artifacts (PAT)
              ┌──────────────▼──┐  ┌──────────▼──────────┐
              │  ACCOUNT 1      │  │  ACCOUNT 2          │
              │  repo yt-ghost  │  │  repo yt-ghost      │
              │  18 runners     │  │  18 runners         │
              │  × N tabs       │  │  × N tabs           │
              └─────────────────┘  └─────────────────────┘
               20-job quota each (Free plan)
```

## 3. Live feed without ngrok (runner-initiated)

No inbound connection to your PC. No Cloudflare account. Flow per runner job:

1. Job starts ghost watcher in background.
2. Job starts tiny frame-relay HTTP server on `127.0.0.1:8899` serving latest screenshots.
3. Job runs `cloudflared tunnel --url http://127.0.0.1:8899` (TryCloudflare quick tunnel —
   no account, random `https://<id>.trycloudflare.com`, per
   [docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare)).
4. Job parses the tunnel URL from cloudflared log, writes `data/tunnel_url.txt`.
5. Upload step ships `tunnel_url.txt` as artifact (already-proven pipeline).
6. Dashboard polls `/api/ci_reports`, extracts tunnel URLs, embeds live frames in grid.
7. Tunnel dies with the job (ephemeral, 6 h max) — matches ghost run lifetime.

Limits: quick tunnels cap ~200 in-flight requests and are dev/test-grade. One tunnel
per runner (not per tab) stays far under the cap. Marketplace precedent:
`AnimMouse/setup-cloudflared` tunnel sub-action.

## 4. Multi-account dispatch

- Dashboard holds `accounts.json`: `[{repo, pat}]`, one PAT per GitHub account.
- Dispatch = `POST /repos/{repo}/actions/workflows/cloud_ghost_watch.yml/dispatches`
  with `inputs: {video_url, tabs_per_runner, stream_target}`.
- Token rule (per [REST docs](https://docs.github.com/en/rest/actions/workflows) +
  community `benc-uk/workflow-dispatch`, `peter-evans/repository-dispatch`):
  same-repo → `GITHUB_TOKEN` suffices; cross-repo/account → PAT with
  **Actions: write** (fine-grained) or `repo` (classic).
- Dashboard `/api/spawn_cloud` already implements single-repo dispatch; extend to
  loop over `accounts.json`. Each account contributes its own 20-job quota:
  2 accounts ≈ 40 concurrent runners.
- Note: `workflow_dispatch` on `push`-triggered workflow coexists fine; inputs
  fall back to defaults (`|| '8'`) on push events.

## 5. Runner-count scaling

Current matrix: 4. Account cap (Free): 20 concurrent.
Recommendation: **18 runners** (leaves 2 slots headroom for other workflows).
Matrix change is one line: `runner_id: [1..18]`.
Combined with ceiling tabs/runner (TBD by autopilot, currently ≥8):
18 × 8 = **144 concurrent tabs per account**, ×2 accounts = **288**.

## 6. Token matrix

| Token | Scope | Used by | For |
|-------|-------|---------|-----|
| `GITHUB_TOKEN` (auto) | repo-local | workflow steps | artifact upload |
| PAT `actions:read` | chosen repos | dashboard | pull artifacts/reports |
| PAT `actions:write` | chosen repos | dashboard | dispatch runs (incl. cross-account) |

Dashboard reads `GITHUB_TOKEN` + `GITHUB_REPOSITORY` env today; extend to
`accounts.json` for N accounts. Never commit tokens — env/file only.

## 7. Research sources

- Runner specs: https://docs.github.com/actions/reference/runners/github-hosted-runners
- Limits (20 concurrent Free / 256 matrix / 6 h): https://docs.github.com/actions/reference/limits
- Quick tunnels: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare
- Dispatch API: https://docs.github.com/en/rest/actions/workflows
- Community: `AnimMouse/setup-cloudflared`, `benc-uk/workflow-dispatch`,
  `peter-evans/repository-dispatch`, `guilouro/multiple-repositories-dispatch`

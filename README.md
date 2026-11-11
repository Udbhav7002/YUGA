# YUGA · CodeGhost

YUGA is an autonomous crash-response pipeline. When a connected service
throws an unhandled exception, a middleware interceptor captures the crash,
scrubs secrets, signs the report, and forwards it to the orchestrator. The
orchestrator generates a reproduction test, runs it in an isolated Docker
sandbox to confirm the bug reproduces, generates a minimal fix, re-runs the
same test to confirm it now passes, and opens a pull request with the raw
output attached. Every fix waits for a human to merge.

```
 unhandled exception
        │
        ▼
 crash interceptor ── HMAC-signed report ──▶ orchestrator
        │ (Presidio scrubs                     │
        │  secrets first)                      ▼
        │                          failing test generated
        │                                       │
        │                                       ▼
        │                                 sandbox · RED run
        │                          (crash must reproduce here)
        │                                       │
        │                                       ▼
        │                                  fix generated
        │                                       │
        │                                       ▼
        │                                 sandbox · GREEN run
        │                            (same test must now pass)
        │                                       │
        └──────────────────────────────────────▶ pull request
                                       verified · or · honest draft
```

## The pipeline

| # | Stage | What happens |
|---|---|---|
| 1 | Crash intercepted | Middleware catches the 500, redacts secrets, signs the report (HMAC-SHA256 over raw bytes), and forwards it. The user still gets their 500. |
| 2 | Reading source | Repository read into memory. No bind mounts, no host-path bugs. |
| 3 | Test generated | Gemini writes a single pytest that reproduces the crash. |
| 4 | Sandbox, red run | The test must fail on the buggy code. A test that cannot fail is regenerated, or the pipeline stops. |
| 5 | Fix generated | Gemini patches the file, after a static blocklist scan. |
| 6 | Sandbox, green run | Identical container, patched code, same test. It must pass. |
| 7 | Pull request | Fix and regression test committed, with raw red/green output and per-stage telemetry in the body. |

## Safety properties

- A fix is never evaluated against a test that cannot fail. If the generated
  test is broken, the pipeline regenerates it or stops.
- Every run happens in an ephemeral container: no network, all capabilities
  dropped, no-new-privileges, 512 MB memory cap, 64 PIDs, 45 s timeout,
  destroyed after each run.
- Generated code is scanned for `os.system`, `subprocess`, `eval(`, `exec(`,
  `__import__` and similar patterns before it is ever executed.
- Microsoft Presidio analyzes stack traces and exception messages; API keys,
  tokens, emails, cards, and wallet addresses are replaced with `<SCRUBBED>`
  before data leaves the process.
- If a fix cannot be verified, the PR opens as a draft labeled
  *UNVERIFIED — needs human review*.
- Merging is always a manual step, verified or not.

## Connecting your own app

CodeGhost is multi-tenant. Sign in at `/login` with GitHub OAuth (or with
the server's offline identity), then open the App Console at `/apps`:

1. Pick one of your repositories, plus the subfolder path if the app does
   not live at the repo root.
2. CodeGhost issues a tenant id and a shared secret. Put them in the app
   you want protected:

   ```
   CODEGHOST_URL=http://<host>:8002/crash?t=<tenant id>
   CODEGHOST_SHARED_SECRET=<tenant secret>
   ```

3. Crashes reported by your app are verified against your secret, the
   source is located from the report itself, and verified fixes open pull
   requests on your repository with your token.

Per-tenant secrets keep one compromised app from exposing another, and
tokens stay server-side. The agent never merges; the PR always waits for a
person.

## Repository layout

```
target_app/       deliberately buggy FastAPI service (the demo target)
middleware/       crash interceptor — HMAC signing + Presidio redaction
orchestrator/     pipeline, Docker sandbox manager, live dashboard (/dash)
agent/            Gemini test & fix generation, blocklist, GitHub delivery
sandbox_image/    pre-warmed pytest container — no network needed at runtime
tools/            operational scripts
```

## Quickstart

```bash
cp .env.example .env          # add GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_REPO
docker compose up --build     # orchestrator :9000 · target app :8000
```

Local development:

```bash
pip install -r requirements.txt
uvicorn orchestrator.main:app --port 8002   # dashboard at /dash
uvicorn target_app.main:app --port 8001
```

## Usage

Trigger the demo crash:

```bash
curl -X POST http://127.0.0.1:8001/calculate \
     -H 'Content-Type: application/json' \
     -d '{"a": 10, "b": 0, "operation": "divide"}'   # 500 → pipeline starts
```

Watch the pipeline at `http://127.0.0.1:8002/dash`, then read the evidence
in the pull request it opens.

## Configuration

See `.env.example`. Key variables: `GEMINI_API_KEY`, `GITHUB_TOKEN`,
`GITHUB_REPO`, `CODEGHOST_SHARED_SECRET`, `SANDBOX_IMAGE`,
`MAX_FIX_ATTEMPTS`, `PIPELINE_DEADLINE`. GitHub login on `/login` also needs
`GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`.

## License

MIT — see [LICENSE](LICENSE).

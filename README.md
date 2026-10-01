# CODEGHOST

**Production crash in → verified fix + pull request out.**

CodeGhost is an autonomous crash-response pipeline. When a service throws an
unhandled exception, a lightweight interceptor captures the crash, signs it,
and ships it to the orchestrator — which generates a reproduction test,
**proves the bug red** in an isolated Docker sandbox, generates a minimal
fix, **proves it green** in the same sandbox, and opens a pull request with
both raw outputs attached.

It is not autocomplete, and it is not ChatGPT in a for-loop. Every change it
ships is one that survived verification — and every fix still waits for a
human to merge.

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
| 1 | **Crash intercepted** | Middleware catches the 500, redacts secrets, signs the report (HMAC-SHA256 over raw bytes), fires it off — the user still gets their 500 |
| 2 | **Reading source** | Repository read into memory — no bind mounts, no host-path bugs |
| 3 | **Test generated** | Gemini writes a single pytest that reproduces the crash |
| 4 | **Sandbox — red run** | Test must **fail** on the buggy code; a test that can't fail disqualifies itself |
| 5 | **Fix generated** | Gemini patches the file; static blocklist scan first |
| 6 | **Sandbox — green run** | Identical container, patched code, same test — must **pass** |
| 7 | **Pull request** | Fix + regression test committed; raw red/green evidence and per-stage telemetry in the body |

## Why judges (and engineers) should believe it

- **Red before green.** A fix is never evaluated against a test that cannot
  fail. If the AI's test is broken, the pipeline regenerates it — or stops.
- **Isolated execution.** Every run happens in an ephemeral container:
  no network, all capabilities dropped, no-new-privileges, 512 MB memory
  cap, 64 PIDs, 45 s timeout, destroyed after each run.
- **Static safety scan.** Generated code is scanned for
  `os.system`, `subprocess`, `eval(`, `exec(`, `__import__` and friends
  before it is ever executed.
- **Secret hygiene.** Microsoft Presidio analyzes stack traces and
  exception messages; API keys, tokens, emails, cards, and wallet
  addresses are replaced with `<SCRUBBED>` before data leaves the process.
- **Honest failure.** If a fix cannot be verified, the PR opens as a
  **draft** labeled *UNVERIFIED — needs human review*. CodeGhost refuses
  to fake success.
- **Human in the loop.** Verified or not, merging is one click — a person's.

## Sign in & connect your app

CodeGhost is multi-tenant. At `/login`, sign in with GitHub (OAuth) — or with
the server's offline identity — then open the **App Console** at `/apps`:

1. Pick one of your repositories (and, if the app lives in a subfolder, the
   path inside the repo).
2. CodeGhost issues a **tenant id + shared secret**. Drop them into the app
   you want protected:

   ```
   CODEGHOST_URL=http://<host>:8002/crash?t=<tenant id>
   CODEGHOST_SHARED_SECRET=<tenant secret>
   ```

3. From that moment, crashes reported by *your* app are HMAC-verified against
   *your* secret, the source is located from the report itself, and verified
   fixes open pull requests on *your* repository with *your* token.

Per-tenant secrets mean one compromised app never exposes another. Tokens
stay server-side. And the boundary holds for every tenant: **the agent never
merges — the last click is always a human's.**

## Repository layout

```
target_app/       deliberately buggy FastAPI service (the victim)
middleware/       crash interceptor — HMAC signing + Presidio redaction
orchestrator/     pipeline, Docker sandbox manager, live dashboard (/dash)
agent/            Gemini test & fix generation, blocklist, GitHub delivery
sandbox_image/    pre-warmed pytest container — no network needed at runtime
tools/            operational scripts
```

## Running it

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

Trigger the rehearsed crash:

```bash
curl -X POST http://127.0.0.1:8001/calculate \
     -H 'Content-Type: application/json' \
     -d '{"a": 10, "b": 0, "operation": "divide"}'   # 500 → pipeline starts
```

Watch the pipeline live at `http://127.0.0.1:8002/dash`, then read the
evidence in the pull request it opens.

## Configuration

See `.env.example`. Key variables: `GEMINI_API_KEY`, `GITHUB_TOKEN`,
`GITHUB_REPO`, `CODEGHOST_SHARED_SECRET`, `SANDBOX_IMAGE`,
`MAX_FIX_ATTEMPTS`, `PIPELINE_DEADLINE`.

---
*Built by Team YUGA. The crash is the demo — the verified pull request is the product.*
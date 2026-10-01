"""
GitHub Delivery — commits fixes and opens Pull Requests.

Uses repo-relative paths (never basename). Supports GITHUB_APP_PREFIX
for monorepo layouts where target_app/ is a subdirectory.
"""

import os
import uuid
from datetime import datetime, timezone

from github import Github

PREFIX = os.getenv("GITHUB_APP_PREFIX", "")


def open_pull_request(
    rel_path: str,
    fix_code: str,
    test_code: str,
    report,
    verified: bool,
    red_output: str = "",
    green_output: str = "",
    job_id: str = "",
    steps: dict | None = None,
) -> str:
    """Create a branch, commit the fix + test, and open a PR.

    Args:
        rel_path: Repo-relative path to the fixed file (e.g. 'main.py').
        fix_code: The complete corrected file content.
        test_code: The regression test content.
        report: CrashReport with crash metadata.
        verified: Whether the fix passed sandbox verification.
        red_output: Raw pytest output proving the test FAILS on buggy code.
        green_output: Raw pytest output proving the test PASSES with the fix.
        job_id: Orchestrator job id (telemetry).
        steps: Per-step timings in seconds (telemetry).

    Returns:
        The HTML URL of the created Pull Request.
    """
    g = Github(os.environ["GITHUB_TOKEN"], timeout=15)
    repo = g.get_repo(os.environ["GITHUB_REPO"])
    base = repo.default_branch
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"codeghost/fix-{stamp}-{uuid.uuid4().hex[:6]}"

    # Create branch from HEAD of default branch
    ref = repo.get_git_ref(f"heads/{base}")
    repo.create_git_ref(f"refs/heads/{branch}", ref.object.sha)

    # Commit the fix at the CORRECT repo-relative path
    gh_path = PREFIX + rel_path
    msg = f"fix: resolve {report.exception_type} in {rel_path}"
    try:
        existing = repo.get_contents(gh_path, ref=branch)
        repo.update_file(
            path=gh_path,
            message=msg,
            content=fix_code,
            sha=existing.sha,
            branch=branch,
        )
    except Exception:
        repo.create_file(
            path=gh_path, message=msg, content=fix_code, branch=branch
        )

    # Commit the regression test
    test_path = f"tests/test_codeghost_{uuid.uuid4().hex[:6]}.py"
    repo.create_file(
        path=test_path,
        message=f"test: regression test for {report.exception_type}",
        content=test_code,
        branch=branch,
    )

    # Open the PR with rich metadata
    badge = "✅ Verified in sandbox" if verified else "⚠️ UNVERIFIED — needs human review"
    timing = ""
    if steps:
        total = round(sum(steps.values()), 1)
        per_step = ", ".join(f"{k}: {v}s" for k, v in steps.items())
        timing = f"\n### Telemetry\n- **Job:** `{job_id}`\n- **Total:** {total}s\n- **Steps:** {per_step}\n"

    pr = repo.create_pull(
        title=f"🤖 CodeGhost: fix {report.exception_type} in {rel_path}",
        body=f"""## Autonomous fix by CodeGhost

**Status:** {badge}

### Crash
- **Route:** `{report.method} {report.route}`
- **Exception:** `{report.exception_type}: {report.exception_message}`
- **Payload:** `{report.payload}`

### What this PR does
Auto-generated a regression test reproducing the crash, then a minimal fix
that was {'**verified to pass**' if verified else '**NOT verified**'} inside
an isolated Docker sandbox (no network, 512MB cap, non-root, all
capabilities dropped, no-new-privileges, 45s timeout, ephemeral container).

### 🔴 RED — test fails on the buggy code
```
{red_output[-2000:]}
```

### 🟢 GREEN — test passes with this fix
```
{green_output[-2000:]}
```
{timing}
<details><summary>Stack trace</summary>

```
{report.stack_trace[:4000]}
```
</details>

---
*Generated automatically by CodeGhost — crash in, PR out. Human stays in the loop: merge is one click.*
""",
        head=branch,
        base=base,
        draft=not verified,
    )

    return pr.html_url

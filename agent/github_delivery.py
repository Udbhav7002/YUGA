"""
GitHub Delivery — commits fixes and opens Pull Requests.

Reviewer-driven model: ONE persistent branch (`codeghost/agent-fixes`)
accumulates every fix as a separate commit — each commit message carries
the job id, exception and route, so every change is traceable to the exact
crash that produced it. A single rolling PR collects one evidence section
per fix (raw red output, raw green output, per-stage telemetry).

Supports GITHUB_APP_PREFIX for monorepo layouts where target_app/ is a
subdirectory.
"""

import os
import threading
import uuid
from datetime import datetime, timezone

from github import Github

PREFIX = os.getenv("GITHUB_APP_PREFIX", "")
BRANCH = os.getenv("CODEGHOST_BRANCH", "codeghost/agent-fixes")
MAX_BODY = 55000

_lock = threading.Lock()


def _evidence_section(rel_path, fix_code_unused, report, verified, red_output,
                      green_output, job_id, steps):
    badge = "✅ Verified in sandbox" if verified else "⚠️ UNVERIFIED — needs human review"
    timing = ""
    if steps:
        total = round(sum(steps.values()), 1)
        per_step = ", ".join(f"{k}: {v}s" for k, v in steps.items())
        timing = f"- **Total:** {total}s · **Steps:** {per_step}\n"
    return f"""## 🤖 `{job_id}` — {report.exception_type} in {rel_path}

**Status:** {badge}
**Commit trail:** `{report.method} {report.route}` · payload `{report.payload}`

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
</details>"""


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
    token: str | None = None,
    repo_name: str | None = None,
    prefix: str | None = None,
):
    """Commit one fix (+ regression test) onto the single rolling branch and
    append its evidence section to the rolling PR.

    token/repo_name/prefix: per-tenant delivery (OAuth user's identity).
    Defaults to the server's .env identity when omitted.

    Returns:
        (pr_html_url, commit_sha, branch)
    """
    with _lock:  # serialize concurrent fixes onto the shared branch
        g = Github(token or os.environ["GITHUB_TOKEN"], timeout=15)
        repo = g.get_repo(repo_name or os.environ["GITHUB_REPO"])
        base = repo.default_branch
        gh_prefix = prefix if prefix is not None else PREFIX

        # ── 1. Single rolling branch: create once, reuse forever ──
        try:
            ref = repo.get_git_ref(f"heads/{BRANCH}")
        except Exception:
            base_ref = repo.get_git_ref(f"heads/{base}")
            repo.create_git_ref(f"refs/heads/{BRANCH}", base_ref.object.sha)
            ref = repo.get_git_ref(f"heads/{BRANCH}")

        # ── 2. Commit the fix — message maps it to the crash ──
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        gh_path = gh_prefix + rel_path
        msg = (f"fix(codeghost): {job_id} — {report.exception_type} in {rel_path} "
               f"({report.method} {report.route})")
        try:
            existing = repo.get_contents(gh_path, ref=BRANCH)
            result = repo.update_file(
                path=gh_path, message=msg, content=fix_code,
                sha=existing.sha, branch=BRANCH,
            )
        except Exception:
            result = repo.create_file(
                path=gh_path, message=msg, content=fix_code, branch=BRANCH,
            )
        commit_sha = result["commit"].sha

        # ── 3. Commit the regression test (suite grows on the branch) ──
        test_path = f"tests/test_codeghost_{uuid.uuid4().hex[:6]}.py"
        repo.create_file(
            path=test_path,
            message=f"test(codeghost): {job_id} — regression for {report.exception_type} ({report.route})",
            content=test_code,
            branch=BRANCH,
        )

        # ── 4. Rolling PR: find the open one for this branch, append ──
        section = _evidence_section(
            rel_path, fix_code, report, verified, red_output,
            green_output, job_id, steps or {},
        )
        rolling = None
        for p in repo.get_pulls(state="open", base=base):
            if p.head.ref == BRANCH:
                rolling = p
                break

        if rolling is not None:
            body = (rolling.body or "") + "\n\n---\n\n" + section
            if len(body) > MAX_BODY:
                parts = body.split("\n\n---\n\n")
                keep = [parts[0]]
                size = len(keep[0])
                for sec in reversed(parts[1:]):
                    if size + len(sec) > MAX_BODY:
                        break
                    keep.insert(1, sec)
                    size += len(sec)
                body = "\n\n---\n\n".join(keep)
            rolling.edit(body=body)
            pr = rolling
        else:
            header = (
                f"# 🤖 CodeGhost — autonomous fixes (rolling)\n\n"
                f"One branch, many fixes. Every commit on `{BRANCH}` is one fix; "
                f"each commit message names the job id and the crash that caused it. "
                f"Sections below map commits to their red→green evidence.\n"
            )
            pr = repo.create_pull(
                title=f"🤖 CodeGhost — autonomous fixes (rolling) · {stamp}",
                body=header + "\n\n---\n\n" + section,
                head=BRANCH,
                base=base,
                draft=not verified,
            )

        return pr.html_url, commit_sha, BRANCH
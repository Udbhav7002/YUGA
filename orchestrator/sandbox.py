"""
Docker Sandbox Manager — the heart of CodeGhost.

Architecture: GLM's create/put_archive/start/wait pattern (no bind mounts).
The orchestrator reads all source files into memory via _read_tree(),
tars them, and injects them into a fresh container via put_archive().
This eliminates all host-path and bind-mount bugs entirely.

Security: no network, all capabilities dropped, no privilege escalation,
non-root user, 512MB memory cap, 64 PIDs, 45s hard timeout, ephemeral
containers destroyed after each run. (Docker 29 blocks put_archive on
read-only rootfs, so isolation comes from the other layers.)
"""

import io
import os
import tarfile
import time

import docker

IMAGE = os.getenv("SANDBOX_IMAGE", "codeghost-sandbox:latest")
TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT", "45"))
PYTEST_CMD = (
    "pytest -q -x --no-header -p no:cacheprovider "
    "--tb=short --disable-warnings /workspace/test_repro.py"
)


class SandboxResult:
    """Wraps pytest exit codes with semantic helpers.
    
    Exit codes: 0=pass, 1=assertion failures, 2=collection/import error,
    3=internal, 4=usage, 5=no tests collected.
    """

    def __init__(self, exit_code: int, output: str):
        self.exit_code = exit_code
        self.output = output

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def test_broken(self) -> bool:
        """True if the test itself is broken (import/collection error),
        as opposed to an assertion failure (which means crash reproduced)."""
        return self.exit_code in (2, 3, 4, 5, -1, -2)


def _tar(files: dict[str, str]) -> io.BytesIO:
    """Create an in-memory tar archive from {filename: content} dict."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


class Sandbox:
    """Manages ephemeral Docker containers for test execution."""

    def __init__(self):
        self.client = docker.from_env()

    def run_test(
        self,
        test_code: str,
        source_tree: dict[str, str],
        patched: dict[str, str] | None = None,
    ) -> SandboxResult:
        """Run a pytest file in an isolated container.

        Args:
            test_code: The pytest file content.
            source_tree: {repo_relative_path: file_content} for all source files.
            patched: Optional overlay — files here replace those in source_tree.
                     Use this for the "green" run with the fix applied.

        Returns:
            SandboxResult with exit_code and captured output.

        Mechanics: the container is created with a keep-alive command and
        started FIRST; then put_archive injects the files through the Docker
        socket and pytest runs via exec. The container is ephemeral — it is
        destroyed (force-removed) after every run.
        """
        tree = {**source_tree, **(patched or {})}
        c = None
        try:
            c = self.client.containers.create(
                image=IMAGE,
                command=["sleep", str(TIMEOUT + 30)],
                network_disabled=True,
                mem_limit="512m",
                pids_limit=64,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                tmpfs={"/tmp": "size=64m,mode=1777"},
                working_dir="/workspace",
            )
            c.start()

            # One archive: test at /workspace/test_repro.py,
            # all source files under /workspace/src/
            files = {"test_repro.py": test_code}
            files.update({f"src/{k}": v for k, v in tree.items()})
            c.put_archive("/workspace", _tar(files))

            exit_code, out = c.exec_run(
                ["sh", "-c", f"timeout {TIMEOUT} {PYTEST_CMD}"],
                environment={
                    "PYTHONPATH": "/workspace/src",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "CODEGHOST_URL": "",  # kill-switch: never phone home from tests
                },
            )
            output = (out or b"").decode("utf-8", errors="replace")
            if exit_code in (124, 137):  # `timeout` killed it
                return SandboxResult(-1, f"SANDBOX TIMEOUT: killed after {TIMEOUT}s\n{output}")
            return SandboxResult(exit_code, output[-4000:])

        except docker.errors.APIError as e:
            return SandboxResult(-2, f"SANDBOX ERROR: {e}")
        finally:
            if c is not None:
                try:
                    c.remove(force=True)
                except Exception:
                    pass

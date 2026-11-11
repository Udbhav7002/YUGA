"""Unit tests for CodeGhost's pure helpers — no Docker, no API keys.

The full pipeline (LLM generation, GitHub delivery, sandbox containers)
needs external services and is exercised by orchestrator/smoke.py and
e2e_test.py locally. Everything here runs in CI exactly as-is.
"""

import tarfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import orchestrator.main as orchestrator_main
from agent.llm_client import _clean, _import_line, _safety_check
from orchestrator.main import (
    CrashReport,
    _fingerprint,
    _read_tree,
    _safe_rel,
    _step,
)
from orchestrator.sandbox import _tar


def _report(**overrides) -> CrashReport:
    base = dict(
        stack_trace=(
            "Traceback (most recent call last):\n"
            '  File "main.py", line 14, in calculate\n'
            "    return a / b\n"
            "ZeroDivisionError: division by zero"
        ),
        exception_type="ZeroDivisionError",
        exception_message="division by zero",
        method="GET",
        route="/calculate",
        source_file_rel="main.py",
    )
    return CrashReport(**{**base, **overrides})


# ── Crash report model ──────────────────────────────────────────────
def test_crash_report_caps_stack_trace_length():
    with pytest.raises(ValidationError):
        _report(stack_trace="x" * 16001)


# ── _fingerprint ────────────────────────────────────────────────────
def test_fingerprint_is_stable_for_identical_crashes():
    assert _fingerprint(_report()) == _fingerprint(_report())


def test_fingerprint_differs_by_exception_type():
    assert _fingerprint(_report()) != _fingerprint(_report(exception_type="TypeError"))


def test_fingerprint_differs_by_traceback_tail():
    a = _report(stack_trace="...\nZeroDivisionError: division by zero")
    b = _report(stack_trace="...\nZeroDivisionError: other message")
    assert _fingerprint(a) != _fingerprint(b)


# ── _safe_rel ───────────────────────────────────────────────────────
def test_safe_rel_accepts_relative_path_under_root(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("x = 1\n")
    monkeypatch.setattr(orchestrator_main, "SOURCE_ROOT", tmp_path)
    assert _safe_rel("main.py", "") == "main.py"


def test_safe_rel_resolves_from_stack_trace(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("x = 1\n")
    monkeypatch.setattr(orchestrator_main, "SOURCE_ROOT", tmp_path)
    trace = 'Traceback (most recent call last):\n  File "/app/main.py", line 3, in calc\n    1/0'
    assert _safe_rel("", trace) == "main.py"


def test_safe_rel_rejects_path_traversal():
    with pytest.raises(HTTPException) as excinfo:
        _safe_rel("../etc/passwd.py", "")
    assert excinfo.value.status_code == 400


def test_safe_rel_rejects_non_python_files():
    with pytest.raises(HTTPException) as excinfo:
        _safe_rel("secrets.txt", "")
    assert excinfo.value.status_code == 400


def test_safe_rel_rejects_unresolvable_crash():
    with pytest.raises(HTTPException) as excinfo:
        _safe_rel("", "no file frames here")
    assert excinfo.value.status_code == 400


def test_safe_rel_404_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator_main, "SOURCE_ROOT", tmp_path)
    with pytest.raises(HTTPException) as excinfo:
        _safe_rel("ghost.py", "")
    assert excinfo.value.status_code == 404


# ── _read_tree ──────────────────────────────────────────────────────
def test_read_tree_reads_py_files_and_skips_cruft(tmp_path):
    (tmp_path / "main.py").write_text("a = 1\n")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("b = 2\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "junk.py").write_text("c = 3\n")
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "dep.py").write_text("d = 4\n")
    tree = _read_tree(tmp_path)
    assert tree == {"main.py": "a = 1\n", "pkg/mod.py": "b = 2\n"}


def test_read_tree_raises_when_no_py_files(tmp_path):
    with pytest.raises(RuntimeError):
        _read_tree(tmp_path)


# ── _step ───────────────────────────────────────────────────────────
def test_step_records_elapsed_seconds():
    steps: dict = {}
    with _step(steps, "read_source"):
        pass
    assert steps["read_source"] >= 0


# ── LLM client helpers ──────────────────────────────────────────────
def test_clean_strips_markdown_fences():
    assert _clean("```python\nprint('hi')\n```") == "print('hi')"


def test_clean_passes_plain_code_through():
    assert _clean("print('hi')") == "print('hi')"


def test_import_line_variants():
    assert _import_line("main.py") == "from main import app"
    assert _import_line("routers/calc.py") == "from routers.calc import app"
    assert _import_line("pkg/__init__.py") == "from pkg import app"


def test_safety_check_flags_dangerous_calls():
    with pytest.raises(ValueError):
        _safety_check("import os\nos.system('ls')", "Test code")


def test_safety_check_allows_safe_code():
    assert _safety_check("result = 1 + 1", "Test code") == "result = 1 + 1"


# ── Sandbox tar helper ──────────────────────────────────────────────
def test_tar_archive_roundtrip():
    buf = _tar({"test_repro.py": "def test_ok(): pass", "src/main.py": "x = 1\n"})
    with tarfile.open(fileobj=buf, mode="r") as tar:
        assert sorted(tar.getnames()) == ["src/main.py", "test_repro.py"]
        content = tar.extractfile("test_repro.py").read().decode()
        assert content == "def test_ok(): pass"


# ── Orchestrator HTTP surface (no external services) ────────────────
def test_health_endpoint():
    client = TestClient(orchestrator_main.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "codeghost-orchestrator"}


def test_status_unknown_job_is_404():
    client = TestClient(orchestrator_main.app)
    assert client.get("/status/does-not-exist").status_code == 404


def test_last_status_with_no_jobs():
    client = TestClient(orchestrator_main.app)
    assert client.get("/status/last").json() == {"crash_id": None}


# ── Presidio scrubber ───────────────────────────────────────────────
def test_scrub_redacts_api_key_and_email():
    from target_app.presidio_scrubber import scrub

    out = scrub("auth failed with api_key=sk-abc123XYZdef456GHI789jkl012 for dev@corp.com")
    assert "sk-abc123XYZdef456GHI789jkl012" not in out
    assert "dev@corp.com" not in out

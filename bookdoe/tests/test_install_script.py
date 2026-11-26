"""install.sh stays silent about indexers and download clients unless BOOKDOE_ACQUISITION_ENABLED is true."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORDS = re.compile(r"indexer|download.?client|acquisition", re.I)


@pytest.fixture
def sandbox(tmp_path):
    if not (ROOT / "install.sh").exists():
        pytest.skip("install.sh not present")
    shutil.copy(ROOT / "install.sh", tmp_path / "install.sh")
    (tmp_path / "bookdoe").mkdir()
    (tmp_path / "openreads").mkdir()
    (tmp_path / "openreads" / ".env.example").write_text("X=1\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    return tmp_path


def _run(sandbox, flag, *args):
    env = {k: v for k, v in os.environ.items() if k != "BOOKDOE_ACQUISITION_ENABLED"}
    env["PATH"] = f"{sandbox / 'bin'}:{env['PATH']}"
    if flag is not None:
        env["BOOKDOE_ACQUISITION_ENABLED"] = flag
    return subprocess.run(["sh", "install.sh", "--no-start", *args], cwd=sandbox, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)


def test_default_run_prints_nothing_about_acquisition(sandbox):
    r = _run(sandbox, None)
    assert r.returncode == 0, r.stderr
    assert not WORDS.search(r.stdout + r.stderr)


def test_false_flag_prints_nothing_about_acquisition(sandbox):
    r = _run(sandbox, "false")
    assert r.returncode == 0, r.stderr
    assert not WORDS.search(r.stdout + r.stderr)


def test_interactive_prompts_skip_the_download_client_question_when_off(sandbox):
    # `ask` prints its prompt only on a terminal; check the prompt text the script would show.
    script = (sandbox / "install.sh").read_text()
    assert 'if acquisition_on; then\n        ask DOWNLOADS_DIR' in script


def test_true_flag_keeps_the_download_client_prompt_path(sandbox):
    r = _run(sandbox, "true")
    assert r.returncode == 0, r.stderr
    assert "DOWNLOADS_DIR=./data/downloads" in (sandbox / ".env").read_text()

"""Demo safety checks; only print mode is allowed to reach dispatch."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/demo.sh"


@pytest.fixture
def demo_env(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    git_log = tmp_path / "git-calls"
    worker_log = tmp_path / "worker-calls"
    git = binaries / "git"
    git.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$GIT_CALLS"\n')
    git.chmod(0o755)
    for name in ("claude", "orca", "orca-ide"):
        worker = binaries / name
        worker.write_text('#!/usr/bin/env bash\necho unexpected-worker >> "$WORKER_CALLS"\nexit 99\n')
        worker.chmod(0o755)
    return {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "PYTHON": sys.executable,
            "GIT_CALLS": str(git_log), "WORKER_CALLS": str(worker_log)}


def run_demo(env, *args):
    assert args[0] not in {"local", "orca"}, "demo tests must never dispatch a live backend"
    result = subprocess.run(["bash", str(SCRIPT), *map(str, args)], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=30)
    assert not Path(env["WORKER_CALLS"]).exists(), "print demo attempted to launch a worker"
    return result


def sources():
    return {str(path.relative_to(REPO)): path.read_bytes()
            for path in (REPO / "workspaces").rglob("*") if path.is_file()}


@pytest.mark.parametrize("destination", ["workspaces", "new-scratch-inside-repo", ".."])
def test_demo_refuses_repository_overlap_without_modifying_sources(demo_env, destination):
    before = sources()
    result = run_demo(demo_env, "print", destination)
    assert result.returncode != 0 and "overlap" in result.stderr
    assert sources() == before
    assert not Path(demo_env["GIT_CALLS"]).exists()
    if destination == "new-scratch-inside-repo":
        assert not (REPO / destination).exists()


def test_demo_refuses_symlink_alias_into_repository(tmp_path, demo_env):
    alias = tmp_path / "alias"
    alias.symlink_to(REPO, target_is_directory=True)
    before = sources()
    result = run_demo(demo_env, "print", alias / "workspaces")
    assert result.returncode != 0 and "overlap" in result.stderr
    assert sources() == before
    assert not Path(demo_env["GIT_CALLS"]).exists()


@pytest.mark.parametrize("with_previous_work", [False, True])
def test_demo_refuses_existing_scratch_including_previous_work(tmp_path, demo_env, with_previous_work):
    scratch = tmp_path / "existing"
    scratch.mkdir()
    previous = scratch / "calc-lib/uncommitted.txt"
    if with_previous_work:
        previous.parent.mkdir()
        previous.write_text("keep this work")
    result = run_demo(demo_env, "print", scratch)
    assert result.returncode != 0 and "already exists" in result.stderr
    assert not Path(demo_env["GIT_CALLS"]).exists()
    if with_previous_work:
        assert previous.read_text() == "keep this work"
    else:
        assert not list(scratch.iterdir())


def test_demo_refuses_symlink_scratch_root(tmp_path, demo_env):
    target = tmp_path / "target"
    alias = tmp_path / "scratch-alias"
    alias.symlink_to(target, target_is_directory=True)
    result = run_demo(demo_env, "print", alias)
    assert result.returncode != 0 and "symlink" in result.stderr
    assert not target.exists()


def test_demo_validates_backend_before_creating_scratch(tmp_path, demo_env):
    scratch = tmp_path / "new"
    result = run_demo(demo_env, "invalid-backend", scratch)
    assert result.returncode == 2 and "invalid backend" in result.stderr
    assert not scratch.exists() and not Path(demo_env["GIT_CALLS"]).exists()


@pytest.mark.parametrize("options", [("--backend", "local"), ("--root", "workspaces"),
                                      ("--allow-tool",), ("--allow-tool", "")])
def test_demo_rejects_invalid_options_before_creating_scratch(tmp_path, demo_env, options):
    scratch = tmp_path / "new"
    result = run_demo(demo_env, "print", scratch, *options)
    assert result.returncode == 2
    assert not scratch.exists() and not Path(demo_env["GIT_CALLS"]).exists()


def print_plans(stdout):
    stream = stdout.split("\n", 1)[1].lstrip()
    decoder = json.JSONDecoder()
    plans = []
    for _ in range(3):
        plan, end = decoder.raw_decode(stream)
        plans.append(plan)
        stream = stream[end:].lstrip()
    assert stream == "print backend: plans only; receipt verification skipped\n"
    return plans


def test_demo_print_copies_hidden_skills_and_skips_verification(tmp_path, demo_env):
    scratch = tmp_path / "space ' new-scratch"
    before = sources()
    result = run_demo(demo_env, "print", scratch, "--accept-edits", "--allow-tool", "Bash(python3 -m pytest -q)")
    assert result.returncode == 0, result.stderr
    plans = print_plans(result.stdout)
    assert [plan["skill"] for plan in plans] == ["add-calc-function", "write-release-note", "summarize-csv"]
    assert len({plan["dispatch_id"] for plan in plans}) == 1
    assert all(Path(plan["folder"]).parent == scratch.resolve() for plan in plans)
    for relative, content in before.items():
        assert (scratch / Path(relative).relative_to("workspaces")).read_bytes() == content
    assert sources() == before
    assert not list(scratch.rglob(".skill-receipts"))
    assert len(Path(demo_env["GIT_CALLS"]).read_text().splitlines()) == 9


def test_demo_print_assigns_a_fresh_id_per_run(tmp_path, demo_env):
    first = run_demo(demo_env, "print", tmp_path / "first")
    second = run_demo(demo_env, "print", tmp_path / "second")
    assert first.returncode == second.returncode == 0
    assert print_plans(first.stdout)[0]["dispatch_id"] != print_plans(second.stdout)[0]["dispatch_id"]


def test_demo_default_scratch_uses_a_fresh_temporary_directory(tmp_path, demo_env):
    # Redirect only tempfile's explicit /tmp destination in the inline helper,
    # while leaving the demo's default-scratch branch and real print dispatch intact.
    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text(
        f'#!{sys.executable}\n'
        'import os, subprocess, sys\n'
        'if sys.argv[1:] == ["-", sys.argv[2], ""]:\n'
        '    source = sys.stdin.read().replace("dir=\\\"/tmp\\\"", f"dir={os.environ[\"DEMO_TEMP\"]!r}")\n'
        '    result = subprocess.run([sys.executable, *sys.argv[1:]], input=source, text=True)\n'
        'else:\n'
        '    result = subprocess.run([sys.executable, *sys.argv[1:]])\n'
        'sys.exit(result.returncode)\n')
    wrapper.chmod(0o755)
    result = run_demo({**demo_env, "PYTHON": str(wrapper), "DEMO_TEMP": str(tmp_path)}, "print")
    assert result.returncode == 0, result.stderr
    plans = print_plans(result.stdout)
    scratch = Path(plans[0]["folder"]).parent
    assert scratch.parent == tmp_path and scratch.name.startswith("skill-dispatch-demo.")

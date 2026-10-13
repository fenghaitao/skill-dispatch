"""Demo safety checks; only print mode is allowed to reach dispatch."""
import json
import os
import shutil
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
    real_git = shutil.which("git")
    assert real_git
    git.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$GIT_CALLS"\nexec "{real_git}" "$@"\n')
    git.chmod(0o755)
    for name in ("claude", "orca", "orca-ide", "herdr"):
        worker = binaries / name
        worker.write_text('#!/usr/bin/env bash\necho unexpected-worker >> "$WORKER_CALLS"\nexit 99\n')
        worker.chmod(0o755)
    return {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "PYTHON": sys.executable,
            "GIT_CALLS": str(git_log), "WORKER_CALLS": str(worker_log)}


def run_demo(env, *args):
    assert args[0] not in {"local", "orca", "herdr"}, "demo tests must never dispatch a live backend"
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
    assert stream == ""
    return plans


def test_demo_print_copies_hidden_skills_and_records_git_identity(tmp_path, demo_env):
    scratch = tmp_path / "space ' new-scratch"
    before = sources()
    result = run_demo(demo_env, "print", scratch, "--accept-edits", "--allow-tool", "Bash(python3 -m pytest -q)")
    assert result.returncode == 0, result.stderr
    plans = print_plans(result.stdout)
    assert [plan["skill"] for plan in plans] == ["add-calc-function", "write-release-note", "summarize-csv"]
    assert all(plan["repo_commit"] and plan["skill_repo_path"] for plan in plans)
    assert all(Path(plan["folder"]).parent == scratch.resolve() for plan in plans)
    for relative, content in before.items():
        assert (scratch / Path(relative).relative_to("workspaces")).read_bytes() == content
    assert sources() == before
    assert len(Path(demo_env["GIT_CALLS"]).read_text().splitlines()) == 24


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


def test_herdr_demo_dispatches_each_task_without_receipt_protocol(tmp_path, demo_env):
    # Stub only the dispatcher entrypoint; keep scratch setup and CLI assembly real.
    wrapper = tmp_path / 'python-dispatch-stub'
    calls = tmp_path / 'dispatch-calls.jsonl'
    wrapper.write_text(
        f'#!{sys.executable}\n'
        'import json, os, subprocess, sys\n'
        'from pathlib import Path\n'
        'if Path(sys.argv[1]).name == "skill_dispatch.py":\n'
        '    with open(os.environ["DISPATCH_CALLS"], "a") as f:\n'
        '        f.write(json.dumps(sys.argv[2:]) + "\\n")\n'
        'else:\n'
        '    sys.exit(subprocess.run([sys.executable, *sys.argv[1:]]).returncode)\n')
    wrapper.chmod(0o755)
    env = {**demo_env, 'PYTHON': str(wrapper), 'DISPATCH_CALLS': str(calls)}
    result = subprocess.run(['bash', str(SCRIPT), 'herdr', str(tmp_path / 'scratch'),
                             '--accept-edits', '--allow-tool', 'Bash(python3 -m pytest -q)'],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not Path(env['WORKER_CALLS']).exists()
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [command[0] for command in commands] == ['dispatch'] * 3
    for command in commands:
        assert command[command.index('--backend') + 1] == 'herdr'
        assert '--wait' not in command
        assert '--dispatch-id' not in command
        assert '--accept-edits' in command
        assert '--allow-tool=Bash(python3 -m pytest -q)' in command

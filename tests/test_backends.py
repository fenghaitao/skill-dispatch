"""Worker backends exercised entirely through mocked subprocess/Orca responses."""
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))
import skill_dispatch as sd

WORKSPACES = Path(__file__).resolve().parents[1] / "workspaces"


@pytest.mark.parametrize("opt_in", [False, True])
def test_local_argv_cwd_permissions_and_quoting(tmp_path, monkeypatch, opt_in):
    calls = []
    folder = tmp_path / "space ' folder"
    folder.mkdir()
    prompt = "Task: $(touch unwanted); 'quoted'\nnext line"
    model = "--dangerously-skip-permissions; $(touch unwanted)"
    rules = ["Read", "Bash(python3 -m pytest -q)"] if opt_in else []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "done", "")

    monkeypatch.setattr(sd.subprocess, "run", run)
    result = sd.dispatch_local(folder, prompt, model, accept_edits=opt_in, allow_tools=rules)
    argv, kwargs = calls[0]
    assert argv == ["claude", "-p", prompt, "--permission-mode",
                    "acceptEdits" if opt_in else "dontAsk",
                    *(["--allowedTools=Read,Bash(python3 -m pytest -q)"] if opt_in else []),
                    f"--model={model}"]
    assert kwargs == {"capture_output": True, "text": True, "cwd": folder.resolve(),
                      "timeout": 900, "stdin": subprocess.DEVNULL}
    assert result == {"backend": "local", "exit": 0, "output": "done"}


def test_local_nonzero_exit_and_stderr_reach_main(monkeypatch, capsys):
    monkeypatch.setattr(sd.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 7, "", "worker failed"))
    assert sd.main(["dispatch", "--root", str(WORKSPACES), "--task", "Summarize sales.csv",
                    "--backend", "local"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["exit"] == 7 and result["output"] == "worker failed"


def test_local_timeout_is_reported(monkeypatch, capsys):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(sd.subprocess, "run", run)
    assert sd.main(["dispatch", "--root", str(WORKSPACES), "--task", "Summarize sales.csv",
                    "--backend", "local"]) == 2
    assert json.loads(capsys.readouterr().out)["state"] == "error"


@pytest.mark.parametrize("register", [False, True])
def test_orca_worktree_uses_exact_folder_and_full_id(tmp_path, monkeypatch, register):
    folder = tmp_path / "workspace"
    folder.mkdir()
    path = str(folder.resolve())
    calls = []
    worktree = f"repo-1::{path}"

    def orca(*args, **kwargs):
        calls.append(args)
        if args == ("repo", "list"):
            return {"repos": ([] if register else [{"id": "repo-1", "path": path}])}
        if args == ("repo", "add", "--path", path):
            return {"repo": {"id": "repo-1", "path": path}}
        assert args == ("worktree", "list", "--repo", "id:repo-1")
        return {"worktrees": [{"id": "wrong", "path": str(tmp_path)},
                              {"id": worktree, "path": path}]}

    monkeypatch.setattr(sd, "_orca", orca)
    assert sd.orca_worktree(folder) == worktree
    assert (("repo", "add", "--path", path) in calls) is register


def test_orca_worktree_refuses_other_checkout(tmp_path, monkeypatch):
    responses = iter([{"repos": [{"id": "repo-1", "path": str(tmp_path)}]},
                      {"worktrees": [{"id": "wrong", "path": str(tmp_path / "other")}]}])
    monkeypatch.setattr(sd, "_orca", lambda *a, **kw: next(responses))
    with pytest.raises(sd.DispatchError, match="no worktree"):
        sd.orca_worktree(tmp_path)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def mock_orca_launch(monkeypatch, folder, handler):
    clock = Clock()
    calls = []
    monkeypatch.setattr(sd.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(sd.time, "sleep", clock.sleep)
    worktree = f"repo-1::{folder.resolve()}"
    monkeypatch.setattr(sd, "orca_worktree", lambda actual:
                        worktree if actual == folder else pytest.fail("wrong worker folder"))

    def orca(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("terminal", "create"):
            assert args[2:4] == ("--worktree", f"id:{worktree}")
            return {"terminal": {"handle": "worker-handle"}}
        assert args[2:4] == ("--terminal", "worker-handle")
        return handler(args, kwargs, clock)

    monkeypatch.setattr(sd, "_orca", orca)
    return clock, calls


@pytest.mark.parametrize("opt_in", [False, True])
def test_orca_waits_through_trust_without_answering_then_sends_once(tmp_path, monkeypatch, capsys, opt_in):
    waits = 0
    prompt = "Task: 'quoted' $(touch unwanted)\nnext line"
    model = "model; $(touch unwanted)"
    receipt = {"accepted": True, "prompt": {"requestId": "request-1", "stages": ["input_accepted"]}}

    def handler(args, kwargs, clock):
        nonlocal waits
        if args[:2] == ("terminal", "wait"):
            waits += 1
            if waits == 1:
                return {"wait": {"satisfied": False, "blockedReason": "agent-cwd-prompt"}}
            return {"wait": {"satisfied": True}}
        assert args == ("terminal", "send", "--terminal", "worker-handle",
                        "--text", prompt, "--enter")
        return {"send": receipt, "warnings": ["turn start unproven"]}

    _, calls = mock_orca_launch(monkeypatch, tmp_path, handler)
    result = sd.dispatch_orca(tmp_path, prompt, "alpha worker", model, 5,
                              accept_edits=opt_in, allow_tools=["Bash(python3 -m pytest -q)"] if opt_in else [])
    command = calls[0][0][-1]
    assert shlex.split(command) == ["claude", "--permission-mode",
                                   "acceptEdits" if opt_in else "dontAsk",
                                   *(["--allowedTools=Bash(python3 -m pytest -q)"] if opt_in else []),
                                   f"--model={model}"]
    assert [args[:2] for args, _ in calls] == [("terminal", "create"), ("terminal", "wait"),
                                              ("terminal", "wait"), ("terminal", "send")]
    assert result["accepted"] is True and result["send"] == receipt
    assert result["warnings"] == ["turn start unproven"]
    assert "agent-cwd-prompt" in capsys.readouterr().err


@pytest.mark.parametrize("satisfied", [False, True])
def test_orca_ready_deadline_bounds_wait_and_prevents_late_send(tmp_path, monkeypatch, satisfied):
    def handler(args, kwargs, clock):
        assert args[:2] == ("terminal", "wait")
        assert args[-2:] == ("--timeout-ms", "1000")
        assert kwargs["timeout"] == 1 + sd.ORCA_WAIT_MARGIN_SECONDS
        clock.now = 1.01
        return {"wait": {"satisfied": satisfied}}

    _, calls = mock_orca_launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match="did not become ready"):
        sd.dispatch_orca(tmp_path, "task", "worker", None, 1)
    assert not any(args[:2] == ("terminal", "send") for args, _ in calls)


def test_orca_hung_wait_has_subprocess_timeout(tmp_path, monkeypatch):
    def handler(args, kwargs, clock):
        raise subprocess.TimeoutExpired("orca", kwargs["timeout"])

    _, calls = mock_orca_launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match="did not become ready.*task not sent"):
        sd.dispatch_orca(tmp_path, "task", "worker", None, 1)
    assert len(calls) == 2


@pytest.mark.parametrize("accepted", [False, None, "true"])
def test_orca_main_rejects_missing_or_negative_send_acknowledgement(monkeypatch, capsys, accepted):
    folder = WORKSPACES / "data-tools"

    def handler(args, kwargs, clock):
        if args[:2] == ("terminal", "wait"):
            return {"wait": {"satisfied": True}}
        return {"send": {} if accepted is None else {"accepted": accepted}}

    mock_orca_launch(monkeypatch, folder, handler)
    assert sd.main(["dispatch", "--root", str(WORKSPACES), "--task", "Summarize sales.csv",
                    "--backend", "orca"]) == 2
    assert "did not accept" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("returncode, payload, succeeds", [
    (0, {"ok": True, "result": {"repos": []}}, True),
    (1, {"ok": True, "result": {"repos": []}}, False),
    (0, {"ok": False, "error": "denied"}, False),
    (0, [], False),
    (0, {"ok": True, "result": None}, False),
])
def test_orca_subprocess_checks_status_and_envelope(monkeypatch, returncode, payload, succeeds):
    def run(argv, **kwargs):
        assert argv == ["orca", "repo", "list", "--json"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 30}
        return subprocess.CompletedProcess(argv, returncode, json.dumps(payload), "")

    monkeypatch.setattr(sd.subprocess, "run", run)
    if succeeds:
        assert sd._orca("repo", "list") == {"repos": []}
    else:
        with pytest.raises(sd.DispatchError):
            sd._orca("repo", "list")


def test_orca_unsatisfied_wait_exit_one_is_an_observation(monkeypatch):
    payload = {"ok": True, "result": {"wait": {"satisfied": False, "blockedReason": "trust"}}}
    monkeypatch.setattr(sd.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 1, json.dumps(payload), ""))
    assert sd._orca("terminal", "wait", timeout=1) == payload["result"]
    with pytest.raises(sd.DispatchError, match="exit 1"):
        sd._orca("terminal", "send")


def test_orca_invalid_json_is_reported(monkeypatch):
    monkeypatch.setattr(sd.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 1, "not json", "CLI failed"))
    with pytest.raises(sd.DispatchError, match="CLI failed"):
        sd._orca("repo", "list")


@pytest.mark.parametrize("option", ["--ready", "--wait"])
@pytest.mark.parametrize("seconds", ["-1", "nan", "inf"])
def test_cli_rejects_invalid_durations(option, seconds):
    with pytest.raises(SystemExit) as exc:
        sd.main(["dispatch", "--root", str(WORKSPACES), "--task", "alpha", f"{option}={seconds}"])
    assert exc.value.code == 2


def test_orca_zero_readiness_budget_starts_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, "orca_worktree", lambda *a: pytest.fail("zero budget launched a worker"))
    with pytest.raises(sd.DispatchError, match="budget"):
        sd.dispatch_orca(tmp_path, "task", "worker", None, 0)


@pytest.mark.parametrize("backend", ["local", "orca", "herdr"])
def test_main_forwards_explicit_permission_opt_ins(monkeypatch, capsys, backend):
    def dispatch(folder, prompt, *args, **kwargs):
        assert folder == (WORKSPACES / "data-tools").resolve()
        assert kwargs == {"accept_edits": True, "allow_tools": ["Read", "Bash(python3 -m pytest -q)"]}
        return {"backend": backend, **({"exit": 0} if backend == "local" else {"accepted": True})}

    monkeypatch.setattr(sd, f"dispatch_{backend}", dispatch)
    if backend == "local":
        def receipt_check(folder, skill, dispatch_id, seconds):
            assert folder == (WORKSPACES / "data-tools").resolve()
            assert skill == "summarize-csv" and seconds == 0
            return True

        monkeypatch.setattr(sd, "wait_for_receipt", receipt_check)
    assert sd.main(["dispatch", "--root", str(WORKSPACES), "--task", "Summarize sales.csv",
                    "--backend", backend, "--accept-edits", "--allow-tool", "Read",
                    "--allow-tool", "Bash(python3 -m pytest -q)"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("transport_timeout", [False, True])
def test_orca_blocked_at_deadline_reports_reason_without_sending(monkeypatch, capsys, transport_timeout):
    folder = WORKSPACES / "data-tools"
    waits = []

    def handler(args, kwargs, clock):
        assert args[:2] == ("terminal", "wait"), "sent a task to a blocked worker"
        wait_ms = int(args[-1])
        assert kwargs["timeout"] == wait_ms / 1000 + sd.ORCA_WAIT_MARGIN_SECONDS
        waits.append(wait_ms)
        if len(waits) == 1:
            clock.now = 0.25
            return {"wait": {"satisfied": False, "blockedReason": "agent-cwd-prompt"}}
        clock.now = 1 + (sd.ORCA_WAIT_MARGIN_SECONDS if transport_timeout else 0) + 0.01
        if transport_timeout:
            raise subprocess.TimeoutExpired("orca", kwargs["timeout"])
        return {"wait": {"satisfied": False, "blockedReason": "agent-cwd-prompt"}}

    _, calls = mock_orca_launch(monkeypatch, folder, handler)
    assert sd.main(["dispatch", "--root", str(WORKSPACES), "--task", "Summarize sales.csv",
                    "--backend", "orca", "--ready", "1"]) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.out)["error"]
    assert error == "claude in worker-handle did not become ready (blocked on agent-cwd-prompt); task not sent"
    assert "blocked on agent-cwd-prompt" in captured.err
    assert len(waits) == 2 and waits[1] < waits[0]
    assert not any(args[:2] == ("terminal", "send") for args, _ in calls)


def test_orca_final_wait_preserves_new_blocked_reason(tmp_path, monkeypatch):
    def handler(args, kwargs, clock):
        clock.now = 1.01
        return {"wait": {"satisfied": False, "blockedReason": "agent-cwd-prompt"}}

    mock_orca_launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match=r"did not become ready \(blocked on agent-cwd-prompt\)"):
        sd.dispatch_orca(tmp_path, "task", "worker", None, 1)


def test_orca_subprocess_margin_is_based_on_wait_chunk(tmp_path, monkeypatch):
    def handler(args, kwargs, clock):
        if args[:2] == ("terminal", "wait"):
            assert args[-2:] == ("--timeout-ms", "30000")
            assert kwargs["timeout"] == 30 + sd.ORCA_WAIT_MARGIN_SECONDS
            return {"wait": {"satisfied": True}}
        return {"send": {"accepted": True}}

    mock_orca_launch(monkeypatch, tmp_path, handler)
    assert sd.dispatch_orca(tmp_path, "task", "worker", None, 120)["accepted"] is True

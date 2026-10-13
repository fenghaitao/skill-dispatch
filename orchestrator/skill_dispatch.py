#!/usr/bin/env python3
"""Discover the skills of several folders and dispatch work to an agent in the right one.

Discovery and routing parse only each skill's frontmatter (name and
description). Dispatch records the selected workspace's Git commit, then starts
a worker agent *in that folder*, so the worker finds the
skill locally. Execution lifecycle and history belong to the backend session.

    skill_dispatch.py discover --root <dir>
    skill_dispatch.py route    --root <dir> --task "..." [--skill NAME]
    skill_dispatch.py dispatch --root <dir> --task "..." [--skill NAME]
                               [--backend print|local|orca|herdr] [--model M]

Backends:
  print  show the plan only;
  local  run `claude -p` as a subprocess with the folder as its working directory;
  orca   open an Orca terminal in the folder, start `claude` there and send the task.
         A trust prompt for a new folder is left to a person; the dispatcher
         names the waiting tab and sends the task once the agent is ready.
  herdr  open a background tab in the caller's Herdr workspace and start Claude
         through the agent API; wait for readiness before submitting the task.

Existing skills are never modified and no receipt protocol is injected into
their tasks. Git identifies the code/skill snapshot; the local process or the
Orca/Herdr session identifies the execution.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

SKILL_GLOB = ".claude/skills/*/SKILL.md"
MAX_FRONTMATTER_BYTES = 16 * 1024
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ORCA_WAIT_MARGIN_SECONDS = 5


class DispatchError(Exception):
    pass


# -- Discovery ---------------------------------------------------------------

class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML with duplicate mapping keys refused instead of overwritten."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in keys:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark)
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


def validate_skill_name(name: str) -> str:
    if not isinstance(name, str) or len(name) > 64 or not SKILL_NAME.fullmatch(name):
        raise DispatchError(f"invalid skill name: {name!r}; use lowercase words separated by hyphens")
    return name


def frontmatter(path: Path) -> dict:
    """Parse at most 16 KiB of YAML, with no read-ahead past the closing line."""
    # FileIO is unbuffered: its readline reads only through the newline. TextIO
    # would decode a whole buffered chunk, including bytes from the skill body.
    with path.open("rb", buffering=0) as handle:
        if handle.read(3) != b"---":
            return {}
        ending = handle.read(1)
        if ending == b"\r":
            ending += handle.read(1)
        if ending not in (b"\n", b"\r\n"):
            return {}
        used = 3 + len(ending)
        header = bytearray()
        while used < MAX_FRONTMATTER_BYTES:
            line = handle.readline(MAX_FRONTMATTER_BYTES - used)
            if not line:
                raise DispatchError(f"{path}: frontmatter is not closed")
            used += len(line)
            # At the byte limit a newline-free prefix may actually be part of
            # a longer line (e.g. ---not-a-delimiter). Do not accept that prefix.
            complete_line = line.endswith(b"\n") or used < MAX_FRONTMATTER_BYTES
            if complete_line and line.rstrip(b"\r\n") == b"---":
                break
            header.extend(line)
        else:
            raise DispatchError(f"{path}: frontmatter exceeds {MAX_FRONTMATTER_BYTES} bytes")
    try:
        fields = yaml.load(header.decode("utf-8"), Loader=UniqueKeyLoader)
    except (yaml.YAMLError, UnicodeError, TypeError) as exc:
        raise DispatchError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(fields, dict):
        raise DispatchError(f"{path}: frontmatter must be a mapping")
    return fields


def discover(root: Path) -> dict:
    """Every folder directly under `root` that has skills, with their frontmatter."""
    folders = []
    candidates = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    for folder in candidates:
        skills = []
        for path in sorted(folder.glob(SKILL_GLOB)):
            meta = frontmatter(path)
            if any(not isinstance(meta.get(key), str) or not meta[key].strip()
                   for key in ("name", "description")):
                raise DispatchError(f"{path}: frontmatter needs name and description")
            validate_skill_name(meta["name"])
            skills.append({"name": meta["name"], "description": meta["description"],
                           "file": path.relative_to(folder).as_posix()})
        if skills:
            folders.append({"folder": folder.name, "path": str(folder.resolve()), "skills": skills})
    names = [s["name"] for f in folders for s in f["skills"]]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise DispatchError(f"skill names are not unique across folders: {duplicates}")
    return {"root": str(root.resolve()), "folders": folders}


# -- Routing -----------------------------------------------------------------

WORD = re.compile(r"[a-z0-9]+")
STOP = {"a", "an", "the", "to", "in", "of", "for", "and", "or", "with", "its", "it",
        "add", "new", "into", "from", "file", "this", "that", "each", "by"}


def _words(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 2}


def route(catalog: dict, task: str, skill: str | None = None) -> dict:
    """The folder and skill for `task`: the named skill, else the best word overlap."""
    entries = [(f, s) for f in catalog["folders"] for s in f["skills"]]
    if skill:
        for folder, entry in entries:
            if entry["name"] == skill:
                return {"folder": folder, "skill": entry, "score": None}
        raise DispatchError(f"no folder has a skill named {skill!r}")
    task_words = _words(task)
    scored = sorted(((len(task_words & _words(f"{s['name']} {s['description']}")), f, s)
                     for f, s in entries), key=lambda item: -item[0])
    if not scored or scored[0][0] == 0:
        raise DispatchError("no skill matches the task; name one with --skill")
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        tied = [s["name"] for score, _, s in scored if score == scored[0][0]]
        raise DispatchError(f"the task matches several skills equally {tied}; name one with --skill")
    score, folder, entry = scored[0]
    return {"folder": folder, "skill": entry, "score": score}


def prompt_for(task: str, skill: str) -> str:
    return (f"Use the `{skill}` skill from this folder for the following task. "
            f"Follow the skill's steps exactly.\n\nTask: {task}")


# -- Dispatch ----------------------------------------------------------------

def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def _git_result(folder: Path, *args: str, timeout: float = 30) -> tuple[int, str, str]:
    """Run Git without a shell; kept separate from worker subprocess mocks."""
    argv = ["git", "-C", str(folder.resolve()), *args]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise DispatchError(f"git {' '.join(args[:2])}: timed out") from exc
    return process.returncode, stdout.strip(), stderr.strip()


def _git(folder: Path, *args: str) -> str:
    code, stdout, stderr = _git_result(folder, *args)
    if code != 0:
        raise DispatchError(f"git {' '.join(args[:2])}: {stderr or stdout or f'exit {code}'}")
    return stdout


def skill_git_identity(folder: Path, skill: dict) -> dict:
    """Committed identity of the selected skill; dirty skill content is refused."""
    repo_root = Path(_git(folder, "rev-parse", "--show-toplevel")).resolve()
    skill_path = (folder.resolve() / skill["file"]).resolve()
    try:
        repo_path = skill_path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise DispatchError(f"selected skill is outside its Git repository: {skill_path}") from exc
    _git(repo_root, "ls-files", "--error-unmatch", "--", repo_path)
    dirty = _git(repo_root, "status", "--porcelain=v1", "--", repo_path)
    if dirty:
        raise DispatchError(f"{skill_path}: selected skill has uncommitted changes; commit it first")
    commit = _git(repo_root, "rev-parse", "HEAD")
    return {"repo_root": str(repo_root), "repo_commit": commit,
            "skill_repo_path": repo_path}


def _orca(*args: str, timeout: float = 30) -> dict:
    result = _run(["orca", *args, "--json"], timeout=timeout)
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise DispatchError(f"orca {args[0]} {args[1] if len(args) > 1 else ''}: "
                            f"{result.stderr.strip() or result.stdout.strip()}") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload, dict) else "invalid response"
        raise DispatchError(f"orca {' '.join(args[:2])}: {error}")
    value = payload.get("result")
    if not isinstance(value, dict):
        raise DispatchError(f"orca {' '.join(args[:2])}: invalid result")
    # An unsatisfied wait is a normal observation with CLI exit 1. Other
    # nonzero exits, including rejected sends, must not become successes.
    unsatisfied_wait = (args[:2] == ("terminal", "wait") and
                        isinstance(value.get("wait"), dict) and
                        value["wait"].get("satisfied") is False)
    if result.returncode != 0 and not (result.returncode == 1 and unsatisfied_wait):
        raise DispatchError(f"orca {' '.join(args[:2])}: exit {result.returncode}; "
                            f"{result.stderr.strip() or value}")
    return value


def orca_worktree(folder: Path) -> str:
    """The Orca worktree id for `folder`, registering the folder as a repo if needed."""
    path = str(folder.resolve())
    repos = _orca("repo", "list").get("repos", [])
    repo = next((r for r in repos if r.get("path") and
                 str(Path(r["path"]).resolve()) == path), None)
    if repo is None:
        repo = _orca("repo", "add", "--path", path).get("repo")
        if not repo:
            raise DispatchError(f"orca repo add returned no repo for {path}")
    worktrees = _orca("worktree", "list", "--repo", f"id:{repo['id']}").get("worktrees", [])
    match = next((w for w in worktrees if w.get("path") and
                  str(Path(w["path"]).resolve()) == path), None)
    if match is None:
        raise DispatchError(f"Orca has no worktree at {path}")
    return match["id"]


def permission_args(accept_edits: bool = False, allow_tools: list[str] | None = None) -> list[str]:
    """Deny unapproved operations by default; wider grants require CLI opt-ins."""
    argv = ["--permission-mode", "acceptEdits" if accept_edits else "dontAsk"]
    if allow_tools:
        argv += [f"--allowedTools={','.join(allow_tools)}"]
    return argv


def validate_seconds(seconds: float) -> float:
    if not math.isfinite(seconds) or seconds < 0:
        raise DispatchError("durations must be finite and nonnegative")
    return seconds


def seconds_argument(value: str) -> float:
    try:
        return validate_seconds(float(value))
    except (ValueError, DispatchError) as exc:
        raise argparse.ArgumentTypeError("duration must be finite and nonnegative") from exc


def dispatch_orca(folder: Path, prompt: str, title: str, model: str | None,
                  ready_seconds: float = 600, *, accept_edits: bool = False,
                  allow_tools: list[str] | None = None) -> dict:
    validate_seconds(ready_seconds)
    if ready_seconds < 0.001:
        raise DispatchError("readiness budget expired; task not sent")
    argv = ["claude", *permission_args(accept_edits, allow_tools)]
    if model:
        argv += [f"--model={model}"]
    command = shlex.join(argv)
    worktree = orca_worktree(folder)
    terminal = _orca("terminal", "create", "--worktree", f"id:{worktree}",
                     "--title", title, "--command", command)["terminal"]["handle"]
    # A folder Claude Code has not been told to trust opens with a trust
    # prompt. That is a person's decision, so the dispatcher never answers it:
    # it says which tab is waiting and keeps waiting for the agent to be ready.
    deadline = time.monotonic() + ready_seconds
    noticed = None

    def readiness_error() -> DispatchError:
        return DispatchError(f"claude in {terminal} did not become ready"
                             f"{f' (blocked on {noticed})' if noticed else ''}; task not sent")

    while True:
        remaining = deadline - time.monotonic()
        if remaining < 0.001:
            raise readiness_error()
        wait_ms = min(30000, int(remaining * 1000))
        try:
            # Let Orca finish its wait and return the blocked reason before
            # subprocess.run kills it. The margin never extends eligibility
            # to send: deadline checks still reject late readiness results.
            wait = _orca("terminal", "wait", "--terminal", terminal, "--for", "tui-idle",
                         "--timeout-ms", str(wait_ms),
                         timeout=wait_ms / 1000 + ORCA_WAIT_MARGIN_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise readiness_error() from exc
        wait = wait.get("wait") or wait
        reason = wait.get("blockedReason")
        if reason and reason != noticed:
            print(f"waiting: Orca tab {title!r} ({terminal}) is blocked on {reason}; "
                  f"answer it there", file=sys.stderr, flush=True)
            noticed = reason
        if time.monotonic() >= deadline:
            raise readiness_error()
        if wait.get("satisfied") is True:
            break
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    if time.monotonic() >= deadline:
        raise readiness_error()
    sent = _orca("terminal", "send", "--terminal", terminal, "--text", prompt, "--enter")
    send = sent.get("send") or sent
    if not isinstance(send, dict) or send.get("accepted") is not True:
        raise DispatchError(f"Orca did not accept the task in {terminal}: {send}")
    return {"backend": "orca", "worktree": worktree, "terminal": terminal,
            "accepted": True, "send": send, "warnings": sent.get("warnings", [])}


def dispatch_local(folder: Path, prompt: str, model: str | None, *,
                   accept_edits: bool = False, allow_tools: list[str] | None = None) -> dict:
    argv = ["claude", "-p", prompt, *permission_args(accept_edits, allow_tools)]
    if model:
        argv += [f"--model={model}"]
    result = _run(argv, cwd=folder.resolve(), timeout=900, stdin=subprocess.DEVNULL)
    return {"backend": "local", "exit": result.returncode,
            "output": (result.stdout or result.stderr).strip()[-2000:]}


class HerdrError(DispatchError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _herdr(*args: str, timeout: float = 30) -> dict:
    """Herdr emits success JSON on stdout and server error JSON on stderr."""
    result = _run(["herdr", *args], timeout=timeout)
    output = (result.stderr or result.stdout) if result.returncode else result.stdout
    try:
        payload = json.loads(output)
    except ValueError as exc:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {output.strip() or 'empty response'}") from exc
    error = payload.get("error") if isinstance(payload, dict) else None
    if result.returncode or error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        raise HerdrError(f"herdr {' '.join(args[:2])}: {error or f'exit {result.returncode}'}", code)
    value = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        raise HerdrError(f"herdr {' '.join(args[:2])}: invalid result")
    return value


def dispatch_herdr(folder: Path, prompt: str, title: str, model: str | None,
                   ready_seconds: float = 600, *, accept_edits: bool = False,
                   allow_tools: list[str] | None = None) -> dict:
    """Create one background tab per task; never answer a blocked worker's UI."""
    validate_seconds(ready_seconds)
    workspace = os.environ.get("HERDR_WORKSPACE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not workspace:
        raise DispatchError("herdr backend requires a Herdr terminal with HERDR_ENV=1 "
                            "and HERDR_WORKSPACE_ID")
    deadline = time.monotonic() + ready_seconds

    def remaining(minimum: float = 0.001) -> float:
        budget = deadline - time.monotonic()
        if budget < minimum:
            raise DispatchError("Herdr readiness budget expired; task not sent")
        return budget

    # Herdr 0.8 requires agent-start timeouts in 3001..300000 milliseconds.
    remaining(3.001)
    created = _herdr("tab", "create", "--workspace", workspace,
                     "--cwd", str(folder.resolve()), "--label", title,
                     "--no-focus", timeout=min(30, remaining()))
    root_pane = created.get("root_pane")
    created_tab = created.get("tab")
    pane = root_pane.get("pane_id") if isinstance(root_pane, dict) else None
    tab = created_tab.get("tab_id") if isinstance(created_tab, dict) else None
    if not isinstance(pane, str) or not pane or not isinstance(tab, str) or not tab:
        raise DispatchError("Herdr tab create returned no tab or root pane")
    name = f"sd-{uuid.uuid4().hex[:20]}"
    argv = permission_args(accept_edits, allow_tools)
    if model:
        argv += [f"--model={model}"]
    try:
        resumed_startup = False
        while True:
            budget = remaining(3.001)
            try:
                started = _herdr("agent", "start", name, "--kind", "claude", "--pane", pane,
                                 "--timeout", str(min(300000, int(budget * 1000))), "--", *argv,
                                 timeout=budget)
                break
            except HerdrError as exc:
                if exc.code == "agent_pane_busy":
                    # A new tab can be returned before its shell is at a prompt.
                    # This explicit rejection starts nothing, so retry only it.
                    time.sleep(min(0.1, remaining()))
                    continue
                if exc.code != "agent_not_ready":
                    raise
                # A blocked startup preserves the named agent. Observe it until a
                # person resolves the prompt; never restart it or send UI input.
                print(f"waiting: Herdr tab {title!r} ({tab}), pane {pane}, agent {name} "
                      "is blocked during startup; answer it there", file=sys.stderr, flush=True)
                resumed_startup = True
                started = _herdr("agent", "get", name, timeout=min(30, remaining()))
                break
        ready_since = None
        while True:
            remaining()
            agent = started.get("agent")
            if (not isinstance(agent, dict) or agent.get("pane_id") != pane
                    or agent.get("agent") != "claude" or agent.get("name") != name):
                raise DispatchError("Herdr returned a different or missing worker; task not sent")
            if (agent.get("agent_status") in {"idle", "done"}
                    and agent.get("interactive_ready") is True
                    and not agent.get("launch_pending", False)):
                if ready_since is None:
                    ready_since = time.monotonic()
                # After a trust dialog, an initial ready screen can be replaced
                # by Claude's startup UI. Require sustained readiness before
                # submitting input.
                if not resumed_startup or time.monotonic() - ready_since >= 3:
                    break
            else:
                ready_since = None
            time.sleep(min(0.1, remaining()))
            started = _herdr("agent", "get", name, timeout=min(30, remaining()))
        remaining()
        # Wait only for observed activity, including a turn that completes
        # quickly. Herdr's prompt activity gate rejects an unchanged idle UI.
        sent = _herdr("agent", "prompt", name, prompt, "--wait",
                      "--until", "working", "--until", "blocked",
                      "--until", "idle", "--until", "done", "--timeout", "10000",
                      timeout=15)
        agent = sent.get("agent")
        if (sent.get("type") != "agent_prompted" or not isinstance(agent, dict)
                or agent.get("pane_id") != pane or agent.get("name") != name
                or agent.get("agent") != "claude"):
            raise DispatchError("Herdr did not acknowledge the task for the expected worker")
        if agent.get("agent_status") not in {"working", "idle", "done"}:
            raise DispatchError("Herdr worker blocked after submission; inspect its prompt")
    except (DispatchError, OSError, subprocess.SubprocessError) as exc:
        raise DispatchError(f"Herdr tab {tab}, pane {pane}, agent {name}: {exc}. "
                            "Inspect this worker before retrying; it has been left open.") from exc
    return {"backend": "herdr", "workspace": workspace, "tab": tab, "pane": pane,
            "agent": name, "accepted": True, "activity_observed": True, "send": sent}


# -- Command line ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "route", "dispatch"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True, type=Path, help="folder holding the workspaces")
        if name in ("route", "dispatch"):
            p.add_argument("--task", required=True)
            p.add_argument("--skill")
        if name == "dispatch":
            p.add_argument("--backend", choices=("print", "local", "orca", "herdr"), default="print")
            p.add_argument("--model")
            p.add_argument("--accept-edits", action="store_true",
                           help="opt in to automatic approval of in-scope edits")
            p.add_argument("--allow-tool", action="append", default=[], metavar="RULE",
                           help="opt in to automatic approval of a Claude tool rule; repeatable")
            p.add_argument("--ready", type=seconds_argument, default=600,
                           help="seconds to wait for the worker to be ready (orca/herdr backends)")
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            print(json.dumps(discover(args.root), indent=2))
            return 0
        choice = route(discover(args.root), args.task, args.skill)
        folder = Path(choice["folder"]["path"])
        skill = choice["skill"]["name"]
        if args.command == "route":
            plan = {"folder": str(folder), "skill": skill, "score": choice["score"],
                    "prompt": prompt_for(args.task, skill)}
            print(json.dumps(plan, indent=2))
            return 0
        git_identity = skill_git_identity(folder, choice["skill"])
        plan = {"folder": str(folder), "skill": skill, "score": choice["score"],
                "prompt": prompt_for(args.task, skill), **git_identity}
        if args.backend == "print":
            print(json.dumps(plan, indent=2))
            return 0
        if args.backend == "local":
            plan.update(dispatch_local(folder, plan["prompt"], args.model,
                                       accept_edits=args.accept_edits, allow_tools=args.allow_tool))
        else:
            dispatch = dispatch_herdr if args.backend == "herdr" else dispatch_orca
            plan.update(dispatch(folder, plan["prompt"], f"{skill} worker", args.model,
                                 args.ready, accept_edits=args.accept_edits,
                                 allow_tools=args.allow_tool))
        print(json.dumps(plan, indent=2))
        return 0 if (plan.get("exit", 0) == 0
                     and plan.get("accepted", True) is True) else 1
    except (DispatchError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())

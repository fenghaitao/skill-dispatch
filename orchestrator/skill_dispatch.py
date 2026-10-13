#!/usr/bin/env python3
"""Discover the skills of several folders and dispatch work to an agent in the right one.

The orchestrator never loads a folder's skills. It reads only each skill's
frontmatter (name and description) to build a catalog, picks the folder whose
skill fits the task, and starts a worker agent *in that folder*, so the
worker's own session finds the skill there.

    skill_dispatch.py discover --root <dir>
    skill_dispatch.py route    --root <dir> --task "..." [--skill NAME]
    skill_dispatch.py dispatch --root <dir> --task "..." [--skill NAME]
                               [--backend print|local|orca] [--model M] [--wait SECONDS]
    skill_dispatch.py verify   --root <dir>

Backends:
  print  show the plan only;
  local  run `claude -p` as a subprocess with the folder as its working directory;
  orca   open an Orca terminal in the folder, start `claude` there and send the task.
         A trust prompt for a new folder is left to a person; the dispatcher
         names the waiting tab and sends the task once the agent is ready.

A skill may ask the worker to write `.skill-receipts/<skill>.json` holding a
token that appears only in the skill's body. `verify` checks those receipts:
a matching token shows the worker read the skill itself, which the
orchestrator never did.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

SKILL_GLOB = ".claude/skills/*/SKILL.md"
RECEIPTS = ".skill-receipts"


class DispatchError(Exception):
    pass


# -- Discovery ---------------------------------------------------------------

def frontmatter(path: Path) -> dict[str, str]:
    """The `key: value` pairs between the leading `---` lines; the body is not read."""
    fields: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        if handle.readline().strip() != "---":
            return fields
        for line in handle:
            if line.strip() == "---":
                return fields
            key, sep, value = line.partition(":")
            if sep and key.strip() and not key.startswith((" ", "\t")):
                fields[key.strip()] = value.strip().strip("'\"")
    raise DispatchError(f"{path}: frontmatter is not closed")


def discover(root: Path) -> dict:
    """Every folder directly under `root` that has skills, with their frontmatter."""
    folders = []
    candidates = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    for folder in candidates:
        skills = []
        for path in sorted(folder.glob(SKILL_GLOB)):
            meta = frontmatter(path)
            if not meta.get("name") or not meta.get("description"):
                raise DispatchError(f"{path}: frontmatter needs name and description")
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
    scored = sorted(((len(task_words & _words(f"{s['name']} {s['description']} {f['folder']}")), f, s)
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
            f"Follow the skill's steps exactly, including any receipt it asks for.\n\nTask: {task}")


# -- Dispatch ----------------------------------------------------------------

def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def _orca(*args: str) -> dict:
    result = _run(["orca", *args, "--json"])
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise DispatchError(f"orca {args[0]} {args[1] if len(args) > 1 else ''}: "
                            f"{result.stderr.strip() or result.stdout.strip()}") from exc
    if not payload.get("ok"):
        raise DispatchError(f"orca {' '.join(args[:2])}: {payload.get('error')}")
    return payload["result"]


def orca_worktree(folder: Path) -> str:
    """The Orca worktree id for `folder`, registering the folder as a repo if needed."""
    path = str(folder.resolve())
    repos = _orca("repo", "list").get("repos", [])
    repo = next((r for r in repos if str(Path(r.get("path", "")).resolve()) == path), None)
    if repo is None:
        repo = _orca("repo", "add", "--path", path).get("repo")
        if not repo:
            raise DispatchError(f"orca repo add returned no repo for {path}")
    worktrees = _orca("worktree", "list", "--repo", f"id:{repo['id']}").get("worktrees", [])
    match = next((w for w in worktrees if str(Path(w.get("path", "")).resolve()) == path), None)
    if match is None:
        raise DispatchError(f"Orca has no worktree at {path}")
    return match["id"]


def dispatch_orca(folder: Path, prompt: str, title: str, model: str | None,
                  ready_seconds: float = 600) -> dict:
    command = "claude --permission-mode acceptEdits"
    if model:
        command += f" --model {shlex.quote(model)}"
    worktree = orca_worktree(folder)
    terminal = _orca("terminal", "create", "--worktree", f"id:{worktree}",
                     "--title", title, "--command", command)["terminal"]["handle"]
    # A folder Claude Code has not been told to trust opens with a trust
    # prompt. That is a person's decision, so the dispatcher never answers it:
    # it says which tab is waiting and keeps waiting for the agent to be ready.
    deadline = time.monotonic() + ready_seconds
    noticed = None
    while True:
        wait = _orca("terminal", "wait", "--terminal", terminal, "--for", "tui-idle",
                     "--timeout-ms", "30000")
        wait = wait.get("wait") or wait
        if wait.get("satisfied"):
            break
        reason = wait.get("blockedReason")
        if reason and reason != noticed:
            print(f"waiting: Orca tab {title!r} ({terminal}) is blocked on {reason}; "
                  f"answer it there", file=sys.stderr, flush=True)
            noticed = reason
        if time.monotonic() > deadline:
            raise DispatchError(f"claude in {terminal} did not become ready"
                                f"{f' (blocked on {reason})' if reason else ''}; task not sent")
    sent = _orca("terminal", "send", "--terminal", terminal, "--text", prompt, "--enter")
    return {"backend": "orca", "worktree": worktree, "terminal": terminal,
            "accepted": bool((sent.get("send") or sent).get("accepted", True))}


def dispatch_local(folder: Path, prompt: str, model: str | None) -> dict:
    argv = ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,Skill"]
    if model:
        argv += ["--model", model]
    result = _run(argv, cwd=folder, timeout=900)
    return {"backend": "local", "exit": result.returncode,
            "output": (result.stdout or result.stderr).strip()[-2000:]}


def wait_for_receipt(folder: Path, skill: str, seconds: float) -> bool:
    receipt = folder / RECEIPTS / f"{skill}.json"
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if receipt.is_file():
            return True
        time.sleep(5)
    return receipt.is_file()


# -- Verification (reads skill bodies; run only after dispatch) ---------------

TOKEN = re.compile(r'"token":\s*"([^"]+)"')


def verify(root: Path) -> list[dict]:
    results = []
    for folder in discover(root)["folders"]:
        base = Path(folder["path"])
        for skill in folder["skills"]:
            receipt = base / RECEIPTS / f"{skill['name']}.json"
            if not receipt.is_file():
                results.append({"skill": skill["name"], "state": "no receipt"})
                continue
            expected = TOKEN.search((base / skill["file"]).read_text(encoding="utf-8"))
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
            except ValueError:
                results.append({"skill": skill["name"], "state": "unreadable receipt"})
                continue
            ok_token = bool(expected) and data.get("token") == expected.group(1)
            ok_cwd = Path(str(data.get("cwd", ""))).resolve() == base.resolve()
            results.append({"skill": skill["name"], "state": "ok" if ok_token and ok_cwd else "mismatch",
                            "token": ok_token, "cwd": ok_cwd})
    return results


# -- Command line ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "route", "dispatch", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True, type=Path, help="folder holding the workspaces")
        if name in ("route", "dispatch"):
            p.add_argument("--task", required=True)
            p.add_argument("--skill")
        if name == "dispatch":
            p.add_argument("--backend", choices=("print", "local", "orca"), default="print")
            p.add_argument("--model")
            p.add_argument("--wait", type=float, default=0,
                           help="seconds to wait for the skill's receipt (orca backend)")
            p.add_argument("--ready", type=float, default=600,
                           help="seconds to wait for the worker to be ready (orca backend)")
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            print(json.dumps(discover(args.root), indent=2))
            return 0
        if args.command == "verify":
            results = verify(args.root)
            print(json.dumps(results, indent=2))
            return 0 if all(r["state"] in ("ok", "no receipt") for r in results) else 1
        choice = route(discover(args.root), args.task, args.skill)
        folder = Path(choice["folder"]["path"])
        skill = choice["skill"]["name"]
        plan = {"folder": str(folder), "skill": skill, "score": choice["score"],
                "prompt": prompt_for(args.task, skill)}
        if args.command == "route" or args.backend == "print":
            print(json.dumps(plan, indent=2))
            return 0
        if args.backend == "local":
            plan.update(dispatch_local(folder, plan["prompt"], args.model))
        else:
            plan.update(dispatch_orca(folder, plan["prompt"], f"{skill} worker", args.model,
                                      args.ready))
            if args.wait:
                plan["receipt"] = wait_for_receipt(folder, skill, args.wait)
        print(json.dumps(plan, indent=2))
        return 0 if plan.get("exit", 0) == 0 and plan.get("receipt", True) else 1
    except (DispatchError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())

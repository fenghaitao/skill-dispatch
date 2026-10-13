#!/usr/bin/env python3
"""Discover the skills of several folders and dispatch work to an agent in the right one.

Discovery, routing and dispatch read only each skill's frontmatter (name and
description). They pick the folder whose skill fits the task and start a
worker agent *in that folder*, so the worker finds the skill locally.
The separate verify command intentionally reads skill bodies to check tokens.

    skill_dispatch.py discover --root <dir>
    skill_dispatch.py route    --root <dir> --task "..." [--skill NAME]
    skill_dispatch.py dispatch --root <dir> --task "..." [--skill NAME]
                               [--backend print|local|orca] [--model M] [--wait SECONDS]
    skill_dispatch.py verify   --root <dir> --dispatch-id ID [--skill NAME ...]

Backends:
  print  show the plan only;
  local  run `claude -p` as a subprocess with the folder as its working directory;
  orca   open an Orca terminal in the folder, start `claude` there and send the task.
         A trust prompt for a new folder is left to a person; the dispatcher
         names the waiting tab and sends the task once the agent is ready.

A skill may ask the worker to write `.skill-receipts/<skill>.json` holding a
token that appears only in the skill's body. Dispatch supplies a dispatch ID;
the worker must include it in an atomically written receipt. Verify checks
the token, skill, absolute working directory and requested dispatch ID.
Receipts are evidence of following instructions, not proof of task completion.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

SKILL_GLOB = ".claude/skills/*/SKILL.md"
RECEIPTS = ".skill-receipts"
MAX_FRONTMATTER_BYTES = 16 * 1024
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DISPATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


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
            if line.rstrip(b"\r\n") == b"---":
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


def validate_dispatch_id(dispatch_id: str) -> str:
    if not isinstance(dispatch_id, str) or not DISPATCH_ID.fullmatch(dispatch_id):
        raise DispatchError("dispatch ID must be 1-128 letters, digits, dots, underscores or hyphens")
    return dispatch_id


def prompt_for(task: str, skill: str, dispatch_id: str | None = None) -> str:
    instruction = (f"Use the `{skill}` skill from this folder for the following task. "
                   "Follow the skill's steps exactly, including any receipt it asks for.")
    if dispatch_id is not None:
        validate_dispatch_id(dispatch_id)
        instruction += (f' Include "dispatch_id": "{dispatch_id}" in the receipt, '
                        "along with the skill, token and absolute cwd. Write the complete JSON "
                        "to a temporary file in the receipt directory and atomically rename it "
                        "to the receipt path after completing the task.")
    return f"{instruction}\n\nTask: {task}"


# -- Dispatch ----------------------------------------------------------------

def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


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
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 0.001:
            raise DispatchError(f"claude in {terminal} did not become ready"
                                f"{f' (blocked on {noticed})' if noticed else ''}; task not sent")
        wait = _orca("terminal", "wait", "--terminal", terminal, "--for", "tui-idle",
                     "--timeout-ms", str(min(30000, int(remaining * 1000))), timeout=remaining)
        wait = wait.get("wait") or wait
        if time.monotonic() >= deadline:
            raise DispatchError(f"claude in {terminal} exceeded readiness budget; task not sent")
        if wait.get("satisfied") is True:
            break
        reason = wait.get("blockedReason")
        if reason and reason != noticed:
            print(f"waiting: Orca tab {title!r} ({terminal}) is blocked on {reason}; "
                  f"answer it there", file=sys.stderr, flush=True)
            noticed = reason
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    if time.monotonic() >= deadline:
        raise DispatchError(f"claude in {terminal} exceeded readiness budget; task not sent")
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


def wait_for_receipt(folder: Path, skill: str, dispatch_id: str, seconds: float) -> bool:
    """Wait for a complete receipt for this dispatch without reading skill bodies."""
    validate_dispatch_id(dispatch_id)
    validate_seconds(seconds)
    deadline = time.monotonic() + seconds
    while True:
        data, _ = read_receipt(folder, skill)
        if data is not None and all(receipt_identity(data, folder, skill, dispatch_id).values()):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


# -- Verification (reads skill bodies; run only after dispatch) ---------------

TOKEN = re.compile(r'"token":\s*"([^"]+)"')


def receipt_path(folder: Path, skill: str) -> Path:
    """Keep receipt lookup within the chosen workspace's receipt directory."""
    validate_skill_name(skill)
    directory = folder.resolve() / RECEIPTS
    receipt = directory / f"{skill}.json"
    try:
        contained = receipt.resolve().is_relative_to(directory)
    except (OSError, RuntimeError) as exc:
        raise DispatchError(f"cannot resolve receipt path: {receipt}") from exc
    if not contained:
        raise DispatchError(f"receipt path escapes {directory}: {receipt}")
    return receipt


def read_receipt(folder: Path, skill: str) -> tuple[dict | None, str]:
    receipt = receipt_path(folder, skill)
    if not receipt.is_file():
        return None, "no receipt"
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "no receipt"
    except (ValueError, UnicodeError, OSError):
        return None, "unreadable receipt"
    if (not isinstance(data, dict) or
            any(not isinstance(data.get(key), str) or not data[key].strip()
                for key in ("skill", "token", "cwd", "dispatch_id")) or
            not Path(data["cwd"]).is_absolute()):
        return None, "invalid receipt"
    return data, "readable"


def receipt_identity(data: dict, folder: Path, skill: str, dispatch_id: str) -> dict[str, bool]:
    try:
        cwd_matches = Path(data["cwd"]).resolve() == folder.resolve()
    except (OSError, RuntimeError, ValueError):
        cwd_matches = False
    return {"cwd": cwd_matches, "identity": data["skill"] == skill,
            "dispatch": data["dispatch_id"] == dispatch_id}


def verify(root: Path, dispatch_id: str, skills: list[str] | None = None) -> list[dict]:
    """Require receipts for selected skills (all discovered skills by default)."""
    validate_dispatch_id(dispatch_id)
    entries = [(folder, skill) for folder in discover(root)["folders"] for skill in folder["skills"]]
    if skills is not None:
        wanted = {validate_skill_name(name) for name in skills}
        unknown = wanted - {skill["name"] for _, skill in entries}
        if unknown:
            raise DispatchError(f"no folder has skills named {sorted(unknown)}")
        entries = [(folder, skill) for folder, skill in entries if skill["name"] in wanted]
    if not entries:
        raise DispatchError("no skills to verify")
    results = []
    for folder, skill in entries:
        base = Path(folder["path"])
        data, state = read_receipt(base, skill["name"])
        if data is None:
            results.append({"skill": skill["name"], "state": state})
            continue
        try:
            expected = TOKEN.search((base / skill["file"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            results.append({"skill": skill["name"], "state": "unreadable skill"})
            continue
        checks = {"token": bool(expected) and data["token"] == expected.group(1),
                  **receipt_identity(data, base, skill["name"], dispatch_id)}
        results.append({"skill": skill["name"], "state": "ok" if all(checks.values()) else "mismatch",
                        **checks})
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
        if name in ("dispatch", "verify"):
            p.add_argument("--dispatch-id", required=name == "verify",
                           help="unique ID for this dispatch; generated if omitted on dispatch")
        if name == "verify":
            p.add_argument("--skill", action="append", help="require receipts only for these skills; repeatable")
        if name == "dispatch":
            p.add_argument("--backend", choices=("print", "local", "orca"), default="print")
            p.add_argument("--model")
            p.add_argument("--accept-edits", action="store_true",
                           help="opt in to automatic approval of in-scope edits")
            p.add_argument("--allow-tool", action="append", default=[], metavar="RULE",
                           help="opt in to automatic approval of a Claude tool rule; repeatable")
            p.add_argument("--wait", type=seconds_argument, default=0,
                           help="seconds to wait for a complete receipt for this dispatch")
            p.add_argument("--ready", type=seconds_argument, default=600,
                           help="seconds to wait for the worker to be ready (orca backend)")
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            print(json.dumps(discover(args.root), indent=2))
            return 0
        if args.command == "verify":
            results = verify(args.root, args.dispatch_id, args.skill)
            print(json.dumps(results, indent=2))
            return 0 if results and all(r["state"] == "ok" for r in results) else 1
        choice = route(discover(args.root), args.task, args.skill)
        folder = Path(choice["folder"]["path"])
        skill = choice["skill"]["name"]
        dispatch_id = (args.dispatch_id or uuid.uuid4().hex) if args.command == "dispatch" else None
        plan = {"folder": str(folder), "skill": skill, "score": choice["score"],
                "prompt": prompt_for(args.task, skill, dispatch_id)}
        if dispatch_id is not None:
            plan["dispatch_id"] = dispatch_id
        if args.command == "route" or args.backend == "print":
            print(json.dumps(plan, indent=2))
            return 0
        if args.backend == "local":
            plan.update(dispatch_local(folder, plan["prompt"], args.model,
                                       accept_edits=args.accept_edits, allow_tools=args.allow_tool))
        else:
            plan.update(dispatch_orca(folder, plan["prompt"], f"{skill} worker", args.model,
                                      args.ready, accept_edits=args.accept_edits,
                                      allow_tools=args.allow_tool))
        if args.wait:
            plan["receipt"] = wait_for_receipt(folder, skill, dispatch_id, args.wait)
        print(json.dumps(plan, indent=2))
        return 0 if (plan.get("exit", 0) == 0 and plan.get("receipt", True)
                     and plan.get("accepted", True) is True) else 1
    except (DispatchError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Documentation contracts for discovery, verification and conservative defaults."""
import re
import shlex
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_qualifies_body_reads_and_initial_skill_visibility():
    text = README.read_text()
    assert "verification intentionally reads skill bodies" in text
    assert "A parent-folder session does not initially load" in text
    assert "can subsequently discover nested skills" in text
    assert "https://code.claude.com/docs/en/skills" in text
    assert "the orchestrator never loads it" not in text


def test_readme_documents_permission_opt_ins_receipt_ids_and_print_demo():
    text = README.read_text()
    for option in ("--permission-mode dontAsk", "--accept-edits", "--allow-tool", "--dispatch-id", "--skill"):
        assert option in text
    assert "Print mode produces plans and explicitly skips receipt verification" in text
    assert "These tests do not establish" in text
    assert "It never deletes an existing" in text


def test_demo_skill_commands_are_covered_by_documented_allow_rules():
    repo = README.parent
    commands = []
    for skill in sorted((repo / "workspaces").glob("*/.claude/skills/*/SKILL.md")):
        # Demo skills spell runnable shell commands as: Run `command`.
        commands.extend((skill, command) for command in
                        re.findall(r"\brun\s+`([^`]+)`", skill.read_text(), re.IGNORECASE))
    assert commands, "no runnable commands found in demo skills"
    demos = [shlex.split(line) for line in README.read_text().splitlines()
             if line.startswith(("scripts/demo.sh local ", "scripts/demo.sh orca "))]
    assert {args[1] for args in demos} == {"local", "orca"}
    for args in demos:
        rules = [args[index + 1] for index, value in enumerate(args) if value == "--allow-tool"]
        for skill, command in commands:
            assert f"Bash({command})" in rules, f"{skill}: {command!r} is not approved by {args}"

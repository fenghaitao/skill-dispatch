"""Documentation contracts for the simplified dispatcher design."""
import re
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
DESIGN = README.parent / "docs/design.md"


def test_readme_documents_git_identity_session_history_and_skill_visibility():
    text = README.read_text()
    assert "repo_commit" in text and "skill_blob" in text
    assert "backend process/session identifies the execution" in text
    assert "There is no dispatcher receipt, synthetic run ID, or verify command" in text
    assert "A parent-folder Claude session does not initially load" in text
    assert "https://code.claude.com/docs/en/skills" in text


def test_readme_documents_permissions_and_cli():
    text = README.read_text()
    for option in ("--permission-mode dontAsk", "--accept-edits", "--allow-tool", "--skill", "--ready"):
        assert option in text
    assert "--dispatch-id" not in text
    assert ".skill-receipts" not in text
    assert "These tests" not in text or "do not" in text


def test_design_keeps_history_with_backends_and_correctness_out_of_dispatcher():
    text = DESIGN.read_text()
    assert "Do not create a separate `dispatch_id` or `run_id`" in text
    assert "backend objects and their logs are the authoritative source" in text
    assert "There is no generic receipt or `verify` command" in text
    assert "Task correctness is domain-specific" in text


def test_demo_skill_commands_have_documented_allow_rules():
    repo = README.parent
    commands = []
    for skill in sorted((repo / "workspaces").glob("*/.claude/skills/*/SKILL.md")):
        commands.extend(command for command in
                        re.findall(r"\brun\s+`([^`]+)`", skill.read_text(), re.IGNORECASE))
    assert commands
    text = README.read_text()
    for command in commands:
        assert f"Bash({command})" in text

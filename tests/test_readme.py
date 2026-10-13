"""Documentation contracts for discovery, verification and conservative defaults."""
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

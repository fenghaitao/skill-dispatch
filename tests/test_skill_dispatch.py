"""Discovery, routing and receipt checks; no agent is started."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))
import skill_dispatch as sd  # noqa: E402

WORKSPACES = Path(__file__).resolve().parents[1] / "workspaces"


def _skill(folder: Path, name: str, description: str, body: str = "") -> Path:
    path = folder / ".claude/skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}")
    return path


def test_discovery_reads_frontmatter_of_every_workspace():
    catalog = sd.discover(WORKSPACES)
    skills = {f["folder"]: [s["name"] for s in f["skills"]] for f in catalog["folders"]}
    assert skills == {"calc-lib": ["add-calc-function"],
                      "data-tools": ["summarize-csv"],
                      "release-notes": ["write-release-note"]}


def test_discovery_never_reads_skill_bodies(tmp_path, monkeypatch):
    _skill(tmp_path / "a", "alpha", "does alpha things", body="SECRET-BODY")
    reads = []
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: reads.append(self) or original(self, *a, **k))
    catalog = sd.discover(tmp_path)
    assert "SECRET-BODY" not in json.dumps(catalog)
    assert reads == []


def test_folders_without_skills_are_skipped(tmp_path):
    (tmp_path / "plain").mkdir()
    _skill(tmp_path / "a", "alpha", "does alpha things")
    assert [f["folder"] for f in sd.discover(tmp_path)["folders"]] == ["a"]


def test_duplicate_skill_names_are_refused(tmp_path):
    _skill(tmp_path / "a", "same", "one")
    _skill(tmp_path / "b", "same", "two")
    with pytest.raises(sd.DispatchError, match="not unique"):
        sd.discover(tmp_path)


def test_unclosed_or_incomplete_frontmatter_is_refused(tmp_path):
    path = tmp_path / "a/.claude/skills/x/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: x\n")
    with pytest.raises(sd.DispatchError, match="not closed"):
        sd.discover(tmp_path)
    path.write_text("---\nname: x\n---\n")
    with pytest.raises(sd.DispatchError, match="name and description"):
        sd.discover(tmp_path)


@pytest.mark.parametrize("task, skill", [
    ("Add a multiply function to calc.py", "add-calc-function"),
    ("Write a release note for version 0.2.0", "write-release-note"),
    ("Summarize sales.csv into a report", "summarize-csv"),
])
def test_tasks_route_to_the_folder_whose_skill_fits(task, skill):
    choice = sd.route(sd.discover(WORKSPACES), task)
    assert choice["skill"]["name"] == skill
    assert Path(choice["folder"]["path"]).name in str(choice["folder"]["path"])


def test_a_named_skill_wins_and_unknown_names_fail():
    catalog = sd.discover(WORKSPACES)
    assert sd.route(catalog, "anything", "summarize-csv")["folder"]["folder"] == "data-tools"
    with pytest.raises(sd.DispatchError, match="no folder"):
        sd.route(catalog, "anything", "missing")


def test_no_match_and_ties_ask_for_a_skill(tmp_path):
    _skill(tmp_path / "a", "alpha", "parse widgets")
    _skill(tmp_path / "b", "beta", "parse gadgets")
    catalog = sd.discover(tmp_path)
    with pytest.raises(sd.DispatchError, match="no skill matches"):
        sd.route(catalog, "bake a cake")
    with pytest.raises(sd.DispatchError, match="equally"):
        sd.route(catalog, "parse things")


def test_print_backend_shows_the_plan_without_starting_an_agent(capsys):
    assert sd.main(["dispatch", "--root", str(WORKSPACES), "--task", "Summarize sales.csv"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["skill"] == "summarize-csv" and plan["folder"].endswith("data-tools")
    assert "`summarize-csv` skill" in plan["prompt"]


def test_verify_checks_receipt_token_and_folder(tmp_path):
    folder = tmp_path / "a"
    _skill(folder, "alpha", "does alpha", body='{"skill": "alpha", "token": "tok-1", "cwd": "<pwd>"}')
    assert sd.verify(tmp_path) == [{"skill": "alpha", "state": "no receipt"}]
    receipt = folder / sd.RECEIPTS / "alpha.json"
    receipt.parent.mkdir()
    receipt.write_text(json.dumps({"skill": "alpha", "token": "tok-1", "cwd": str(folder)}))
    assert sd.verify(tmp_path)[0]["state"] == "ok"
    receipt.write_text(json.dumps({"skill": "alpha", "token": "guess", "cwd": str(folder)}))
    assert sd.verify(tmp_path)[0] == {"skill": "alpha", "state": "mismatch", "token": False, "cwd": True}

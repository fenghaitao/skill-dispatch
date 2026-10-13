"""Discovery, routing and Git identity checks; no agent is started."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))
import skill_dispatch as sd  # noqa: E402

WORKSPACES = Path(__file__).resolve().parents[1] / "workspaces"


def _commit(repo: Path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-q", "-m", "initial"], cwd=repo, check=True)


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


def test_discovery_never_reads_skill_bodies(tmp_path, monkeypatch, capsys):
    path = _skill(tmp_path / "a", "alpha", "does alpha things")
    header = path.read_bytes()
    boundary = header.index(b"\n---\n", 4) + len(b"\n---\n")
    path.write_bytes(header[:boundary] + b"SECRET-BODY\xff\xfe")
    _commit(tmp_path)
    original = Path.open
    positions = []

    class HeaderOnly:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def read(self, size):
            data = self.handle.read(size)
            positions.append(self.handle.tell())
            assert positions[-1] <= boundary
            return data

        def readline(self, size):
            data = self.handle.readline(size)
            positions.append(self.handle.tell())
            assert positions[-1] <= boundary
            return data

    def open_header(self, *args, **kwargs):
        assert args == ("rb",) and kwargs == {"buffering": 0}
        return HeaderOnly(original(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", open_header)
    catalog = sd.discover(tmp_path)
    assert "SECRET-BODY" not in json.dumps(catalog)
    assert positions[-1] == boundary
    for command in ("route", "dispatch"):
        assert sd.main([command, "--root", str(tmp_path), "--task", "alpha"]) == 0
    capsys.readouterr()


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


@pytest.mark.parametrize("task, skill, folder", [
    ("Add a multiply function to calc.py", "add-calc-function", "calc-lib"),
    ("Write a release note for version 0.2.0", "write-release-note", "release-notes"),
    ("Summarize sales.csv into a report", "summarize-csv", "data-tools"),
])
def test_tasks_route_to_the_folder_whose_skill_fits(task, skill, folder):
    choice = sd.route(sd.discover(WORKSPACES), task)
    assert choice["skill"]["name"] == skill
    assert Path(choice["folder"]["path"]) == (WORKSPACES / folder).resolve()


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


def test_print_backend_shows_the_plan_without_starting_an_agent(capsys, monkeypatch):
    monkeypatch.setattr(sd, "_run", lambda *a, **kw: pytest.fail("print backend launched a process"))
    assert sd.main(["dispatch", "--root", str(WORKSPACES), "--task", "Summarize sales.csv"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["skill"] == "summarize-csv" and plan["folder"].endswith("data-tools")
    assert "`summarize-csv` skill" in plan["prompt"]


def test_git_identity_records_commit_and_skill_blob_and_rejects_dirty_skill(tmp_path):
    folder = tmp_path / "a"
    skill_path = _skill(folder, "alpha", "does alpha", body="# Alpha\n")
    _commit(folder)
    entry = sd.discover(tmp_path)["folders"][0]["skills"][0]
    identity = sd.skill_git_identity(folder, entry)
    assert identity["repo_root"] == str(folder.resolve())
    assert identity["repo_commit"]
    assert identity["skill_repo_path"] == ".claude/skills/alpha/SKILL.md"

    (folder / "unrelated.txt").write_text("dirty but unrelated")
    assert sd.skill_git_identity(folder, entry) == identity

    skill_path.write_text(skill_path.read_text() + "\nchanged instructions\n")
    with pytest.raises(sd.DispatchError, match="uncommitted changes"):
        sd.skill_git_identity(folder, entry)


@pytest.mark.parametrize("description, expected", [
    (">-\n  summarize widgets\n  into reports", "summarize widgets into reports"),
    ("|\n  summarize widgets\n  into reports", "summarize widgets\ninto reports\n"),
    ('"summarize \\"widgets\\"" # comment', 'summarize "widgets"'),
    ("'summarize user''s widgets'", "summarize user's widgets"),
])
def test_yaml_frontmatter_routes_block_and_quoted_descriptions(tmp_path, description, expected):
    _skill(tmp_path / "a", "alpha", description)
    catalog = sd.discover(tmp_path)
    assert catalog["folders"][0]["skills"][0]["description"] == expected
    assert sd.route(catalog, "summarize widgets")["skill"]["name"] == "alpha"


@pytest.mark.parametrize("header, message", [
    ("name: alpha\nname: beta\ndescription: things", "duplicate key"),
    ("name: alpha\ndescription: [unclosed", "invalid YAML"),
    ("- name\n- description", "mapping"),
    ("name: alpha\ndescription: []", "name and description"),
    ("name: alpha\ndescription: ' '", "name and description"),
    ("name: true\ndescription: things", "name and description"),
    ("name: alpha\ndescription: !!python/object:os.system {}", "invalid YAML"),
])
def test_invalid_yaml_frontmatter_is_refused(tmp_path, header, message):
    path = _skill(tmp_path / "a", "alpha", "things")
    path.write_text(f"---\n{header}\n---\n")
    with pytest.raises(sd.DispatchError, match=message):
        sd.discover(tmp_path)


def test_frontmatter_supports_crlf(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"---\r\nname: alpha\r\ndescription: things\r\n---\r\n\xff")
    assert sd.frontmatter(path) == {"name": "alpha", "description": "things"}


def test_unterminated_header_read_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"---\n" + b"x" * (sd.MAX_FRONTMATTER_BYTES * 2))
    # Count raw reads independently of the parser's error text.
    import io
    read_positions = []

    class TrackingFile(io.FileIO):
        def read(self, size=-1):
            data = super().read(size)
            read_positions.append(self.tell())
            return data

        def readline(self, size=-1):
            data = super().readline(size)
            read_positions.append(self.tell())
            return data

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: TrackingFile(self, "r"))
    with pytest.raises(sd.DispatchError, match="exceeds"):
        sd.frontmatter(path)
    assert max(read_positions) == sd.MAX_FRONTMATTER_BYTES


@pytest.mark.parametrize("name", ["../other", "/tmp/other", "a/b", "alpha\\beta", "Alpha", "a" * 65])
def test_unsafe_skill_names_are_refused(tmp_path, name):
    path = _skill(tmp_path / "a", "alpha", "things")
    path.write_text(f"---\nname: {json.dumps(name)}\ndescription: things\n---\n")
    with pytest.raises(sd.DispatchError, match="invalid skill name"):
        sd.discover(tmp_path)


def test_folder_names_do_not_affect_routing_or_ties(tmp_path):
    _skill(tmp_path / "reports", "alpha", "parse widgets")
    _skill(tmp_path / "widgets", "beta", "parse gadgets")
    catalog = sd.discover(tmp_path)
    with pytest.raises(sd.DispatchError, match="no skill matches"):
        sd.route(catalog, "reports")
    assert sd.route(catalog, "widgets")["skill"]["name"] == "alpha"
    with pytest.raises(sd.DispatchError, match="equally"):
        sd.route(catalog, "parse")


def test_header_limit_cannot_turn_a_truncated_prefix_into_a_delimiter(tmp_path, monkeypatch):
    path = tmp_path / "SKILL.md"
    header = b"---\nname: alpha\ndescription: widgets\n"
    path.write_bytes(header + b"---not-a-delimiter\nbody")
    monkeypatch.setattr(sd, "MAX_FRONTMATTER_BYTES", len(header) + 3)
    with pytest.raises(sd.DispatchError, match="exceeds"):
        sd.frontmatter(path)


def test_header_closing_delimiter_at_eof_is_accepted_below_limit(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"---\nname: alpha\ndescription: widgets\n---")
    assert sd.frontmatter(path) == {"name": "alpha", "description": "widgets"}

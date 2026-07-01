"""AgentProfile and first-class Skill routing tests."""

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ariadne.api import app
from ariadne.briefing import generate_briefing
from ariadne.cli import app as cli_app
from ariadne.models import AgentProfileStatus, AssigneeType
from ariadne.store import Store


def test_agent_profile_skill_binding_drives_briefing_and_handoff(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    try:
        skill = store.create_skill(
            name="python-fix",
            description="Python implementation and tests",
            when_to_use="Use for Python code changes.",
            prompt_snippet="Always run targeted pytest before handoff.",
            tools_allowed=["pytest", "ruff"],
            test_command="uv run pytest -q",
            source_path="/skills/python-fix/SKILL.md",
            version="1.0.0",
        )
        profile = store.create_agent_profile(
            name="Coder",
            description="Implementation teammate",
            instructions="Write small, tested patches.",
            preferred_capabilities=["dry-run"],
            runtime_policy={"confirm_execution": False},
            max_concurrent_taskruns=2,
        )
        store.bind_skill_to_agent_profile(profile.id, skill.id)

        assert profile.status == AgentProfileStatus.ACTIVE
        assert store.list_skills_for_agent_profile(profile.id) == [skill]

        legacy_agent = store.get_agent(profile.id)
        assert legacy_agent is not None
        assert legacy_agent.name == "Coder"
        assert legacy_agent.backends == ["dry-run"]
        assert legacy_agent.skills == ["python-fix"]

        leader = store.create_agent("Leader", "Coordinate", ["dry-run"], ["planning"])
        legacy_member = store.create_agent(
            "Legacy Reviewer", "Review", ["dry-run"], ["legacy-review"]
        )
        squad = store.create_squad("Runtime Team", leader.id)
        store.add_squad_member(squad.id, profile.id, role="coder")
        store.add_squad_member(squad.id, legacy_member.id, role="reviewer")

        briefing = generate_briefing(store, squad.id)
        coder = next(entry for entry in briefing.roster if entry.agent_id == profile.id)
        reviewer = next(
            entry for entry in briefing.roster if entry.agent_id == legacy_member.id
        )
        assert coder.skills == ["python-fix"]
        assert coder.backends == ["dry-run"]
        assert reviewer.skills == ["legacy-review"]

        issue = store.create_issue("Fix parser", "The parser crashes.", AssigneeType.AGENT, profile.id)
        taskrun = store.enqueue_taskrun(issue.id, profile.id, handoff_prompt="Fix the parser.")

        assert taskrun.handoff_prompt is not None
        assert "Fix the parser." in taskrun.handoff_prompt
        assert "Skill capability package:" in taskrun.handoff_prompt
        assert "### python-fix" in taskrun.handoff_prompt
        assert "python-fix" in taskrun.handoff_prompt
        assert "Allowed tools: pytest, ruff" in taskrun.handoff_prompt
        assert "Verification command: uv run pytest -q" in taskrun.handoff_prompt
        assert "Always run targeted pytest before handoff." in taskrun.handoff_prompt
    finally:
        store.close()


def test_file_backed_store_enables_wal_journal_mode(tmp_path):
    db_path = tmp_path / "wal.db"
    store = Store(str(db_path))
    try:
        row = store._conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0].lower() == "wal"
    finally:
        store.close()


def test_agent_profile_skill_cli_api_and_dashboard_surfaces(tmp_path, monkeypatch):
    db = str(tmp_path / "cli-api.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)
    monkeypatch.setattr("ariadne.api._db_path", db)
    runner = CliRunner()

    result = runner.invoke(
        cli_app,
        [
            "skill-create",
            "--name",
            "python-fix",
            "--description",
            "Python implementation and tests",
            "--when-to-use",
            "Use for Python code changes.",
            "--prompt-snippet",
            "Run pytest before handoff.",
            "--tool",
            "pytest",
            "--test-command",
            "uv run pytest -q",
            "--source-path",
            "/skills/python-fix/SKILL.md",
            "--version",
            "1.0.0",
        ],
    )
    assert result.exit_code == 0
    assert "Created skill:" in result.stdout
    skill_id = result.stdout.split()[2]

    result = runner.invoke(
        cli_app,
        [
            "agent-profile-create",
            "--name",
            "Coder",
            "--description",
            "Implementation teammate",
            "--instructions",
            "Write small patches.",
            "--capability",
            "dry-run",
            "--runtime-policy",
            '{"confirm_execution": false}',
            "--max-concurrent-taskruns",
            "2",
        ],
    )
    assert result.exit_code == 0
    assert "Created agent profile:" in result.stdout
    profile_id = result.stdout.split()[3]

    result = runner.invoke(cli_app, ["agent-profile-bind-skill", profile_id, "python-fix"])
    assert result.exit_code == 0
    assert f"Bound skill {skill_id}" in result.stdout

    result = runner.invoke(cli_app, ["agent-profile-list"])
    assert result.exit_code == 0
    assert profile_id in result.stdout
    assert "Coder" in result.stdout
    assert "python-fix" in result.stdout

    result = runner.invoke(cli_app, ["skill-list"])
    assert result.exit_code == 0
    assert skill_id in result.stdout
    assert "python-fix" in result.stdout

    client = TestClient(app)
    res = client.get("/api/agent-profiles")
    assert res.status_code == 200
    assert res.json() == [
        {
            "id": profile_id,
            "name": "Coder",
            "description": "Implementation teammate",
            "instructions": "Write small patches.",
            "preferred_capabilities": ["dry-run"],
            "runtime_policy": {"confirm_execution": False},
            "max_concurrent_taskruns": 2,
            "status": "active",
            "skills": ["python-fix"],
        }
    ]

    res = client.get(f"/api/agent-profiles/{profile_id}")
    assert res.status_code == 200
    assert res.json()["skills"] == ["python-fix"]

    res = client.get("/api/skills")
    assert res.status_code == 200
    assert res.json()[0]["name"] == "python-fix"
    assert res.json()[0]["tools_allowed"] == ["pytest"]

    res = client.get(f"/api/skills/{skill_id}")
    assert res.status_code == 200
    assert res.json()["prompt_snippet"] == "Run pytest before handoff."

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "AgentProfiles" in dashboard.text
    assert "Skills" in dashboard.text

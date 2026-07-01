"""AgentProfile, Skill, and legacy Agent persistence methods."""

from __future__ import annotations

import json
from datetime import datetime

from ariadne.models import Agent, AgentProfile, AgentProfileStatus, Skill

from .base import DEFAULT_AGENT_PROFILE_MAX_CONCURRENT_TASKRUNS, _new_id, _now_iso


class SkillRepo:

    # ------------------------------------------------------------------
    # AgentProfile / Skill / Agent
    # ------------------------------------------------------------------

    def create_agent_profile(
        self,
        name: str,
        description: str = "",
        instructions: str = "",
        preferred_capabilities: list[str] | None = None,
        runtime_policy: dict | None = None,
        max_concurrent_taskruns: int = DEFAULT_AGENT_PROFILE_MAX_CONCURRENT_TASKRUNS,
        status: AgentProfileStatus = AgentProfileStatus.ACTIVE,
    ) -> AgentProfile:
        now = _now_iso()
        profile = AgentProfile(
            id=_new_id("profile"),
            name=name,
            description=description,
            instructions=instructions,
            preferred_capabilities=preferred_capabilities or [],
            runtime_policy=runtime_policy or {},
            max_concurrent_taskruns=max_concurrent_taskruns,
            status=status,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )
        self._conn.execute(
            """INSERT INTO agent_profile
               (id, name, description, instructions, preferred_capabilities_json,
                runtime_policy_json, max_concurrent_taskruns, status, created_at,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile.id,
                profile.name,
                profile.description,
                profile.instructions,
                json.dumps(profile.preferred_capabilities),
                json.dumps(profile.runtime_policy),
                profile.max_concurrent_taskruns,
                profile.status.value,
                profile.created_at.isoformat(),
                profile.updated_at.isoformat(),
            ),
        )
        self._conn.execute(
            """INSERT INTO agent (id, name, instructions, backends, skills)
               VALUES (?, ?, ?, ?, ?)""",
            (
                profile.id,
                profile.name,
                profile.instructions,
                json.dumps(profile.preferred_capabilities),
                "[]",
            ),
        )
        self._conn.commit()
        return profile

    def get_agent_profile(self, agent_profile_id: str) -> AgentProfile | None:
        row = self._conn.execute(
            "SELECT * FROM agent_profile WHERE id = ?", (agent_profile_id,)
        ).fetchone()
        return self.row_to(AgentProfile, row) if row else None

    def list_agent_profiles(self) -> list[AgentProfile]:
        rows = self._conn.execute(
            "SELECT * FROM agent_profile ORDER BY name"
        ).fetchall()
        return [self.row_to(AgentProfile, r) for r in rows]

    def create_skill(
        self,
        name: str,
        description: str = "",
        when_to_use: str = "",
        prompt_snippet: str = "",
        tools_allowed: list[str] | None = None,
        test_command: str | None = None,
        source_path: str | None = None,
        version: str = "",
    ) -> Skill:
        now = _now_iso()
        skill = Skill(
            id=_new_id("skill"),
            name=name,
            description=description,
            when_to_use=when_to_use,
            prompt_snippet=prompt_snippet,
            tools_allowed=tools_allowed or [],
            test_command=test_command,
            source_path=source_path,
            version=version,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )
        self._conn.execute(
            """INSERT INTO skill
               (id, name, description, when_to_use, prompt_snippet,
                tools_allowed_json, test_command, source_path, version,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                skill.id,
                skill.name,
                skill.description,
                skill.when_to_use,
                skill.prompt_snippet,
                json.dumps(skill.tools_allowed),
                skill.test_command,
                skill.source_path,
                skill.version,
                skill.created_at.isoformat(),
                skill.updated_at.isoformat(),
            ),
        )
        self._conn.commit()
        return skill

    def get_skill(self, skill_id: str) -> Skill | None:
        row = self._conn.execute(
            "SELECT * FROM skill WHERE id = ?", (skill_id,)
        ).fetchone()
        return self.row_to(Skill, row) if row else None

    def get_skill_by_name(self, name: str) -> Skill | None:
        row = self._conn.execute(
            "SELECT * FROM skill WHERE name = ?", (name,)
        ).fetchone()
        return self.row_to(Skill, row) if row else None

    def resolve_skill(self, skill_id_or_name: str) -> Skill | None:
        return self.get_skill(skill_id_or_name) or self.get_skill_by_name(skill_id_or_name)

    def list_skills(self) -> list[Skill]:
        rows = self._conn.execute("SELECT * FROM skill ORDER BY name").fetchall()
        return [self.row_to(Skill, r) for r in rows]

    def bind_skill_to_agent_profile(
        self, agent_profile_id: str, skill_id_or_name: str
    ) -> Skill:
        profile = self.get_agent_profile(agent_profile_id)
        if profile is None:
            raise KeyError(f"agent profile not found: {agent_profile_id}")
        skill = self.resolve_skill(skill_id_or_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_id_or_name}")
        self._conn.execute(
            """INSERT OR IGNORE INTO agent_profile_skill
               (agent_profile_id, skill_id, created_at)
               VALUES (?, ?, ?)""",
            (agent_profile_id, skill.id, _now_iso()),
        )
        self._conn.commit()
        self._sync_legacy_agent_from_profile(agent_profile_id)
        return skill

    def list_skills_for_agent_profile(self, agent_profile_id: str) -> list[Skill]:
        rows = self._conn.execute(
            """SELECT skill.*
               FROM skill
               JOIN agent_profile_skill ON agent_profile_skill.skill_id = skill.id
               WHERE agent_profile_skill.agent_profile_id = ?
               ORDER BY skill.name""",
            (agent_profile_id,),
        ).fetchall()
        return [self.row_to(Skill, r) for r in rows]

    def _sync_legacy_agent_from_profile(self, agent_profile_id: str) -> None:
        profile = self.get_agent_profile(agent_profile_id)
        if profile is None:
            raise KeyError(f"agent profile not found: {agent_profile_id}")
        skill_names = [skill.name for skill in self.list_skills_for_agent_profile(profile.id)]
        self._conn.execute(
            """INSERT INTO agent (id, name, instructions, backends, skills)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    instructions = excluded.instructions,
                    backends = excluded.backends,
                    skills = excluded.skills""",
            (
                profile.id,
                profile.name,
                profile.instructions,
                json.dumps(profile.preferred_capabilities),
                json.dumps(skill_names),
            ),
        )
        self._conn.commit()

    def _compose_taskrun_handoff(
        self, agent_profile_id: str, handoff_prompt: str | None
    ) -> str | None:
        skills = self.list_skills_for_agent_profile(agent_profile_id)
        blocks = []
        for skill in skills:
            lines = [f"### {skill.name}"]
            if skill.description:
                lines.append(f"Routing description: {skill.description}")
            if skill.when_to_use:
                lines.append(f"When to use: {skill.when_to_use}")
            if skill.prompt_snippet:
                lines.append(f"Prompt content: {skill.prompt_snippet}")
            if skill.tools_allowed:
                lines.append(f"Allowed tools: {', '.join(skill.tools_allowed)}")
            if skill.test_command:
                lines.append(f"Verification command: {skill.test_command}")
            blocks.append("\n".join(lines))
        if not blocks:
            return handoff_prompt
        section = "Skill capability package:\n" + "\n\n".join(blocks)
        if not handoff_prompt:
            return section
        return f"{handoff_prompt}\n\n{section}"

    def create_agent(
        self,
        name: str,
        instructions: str,
        backends: list[str],
        skills: list[str],
    ) -> Agent:
        agent = Agent(
            id=_new_id("agent"),
            name=name,
            instructions=instructions,
            backends=backends,
            skills=skills,
        )
        self._conn.execute(
            """INSERT INTO agent (id, name, instructions, backends, skills)
               VALUES (?, ?, ?, ?, ?)""",
            (
                agent.id,
                agent.name,
                agent.instructions,
                json.dumps(agent.backends),
                json.dumps(agent.skills),
            ),
        )
        self._conn.commit()
        return agent

    def get_agent(self, agent_id: str) -> Agent | None:
        row = self._conn.execute(
            "SELECT * FROM agent WHERE id = ?", (agent_id,)
        ).fetchone()
        return self.row_to(Agent, row) if row else None

    def get_agent_by_name(self, name: str) -> Agent | None:
        row = self._conn.execute(
            "SELECT * FROM agent WHERE name = ? ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        return self.row_to(Agent, row) if row else None

    def list_agents(self) -> list[Agent]:
        rows = self._conn.execute("SELECT * FROM agent ORDER BY name").fetchall()
        return [self.row_to(Agent, r) for r in rows]

"""LLM-backed delegation decision for Squad leader.

Calls an OpenAI-compatible LLM with the briefing + issue, parses the
response into a DelegationDecision. Falls back to deterministic_decide
when no API key is available.

Per docs/plan/tasks/squad-003.md.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from ariadne.models import DelegationDecision, Issue, SquadBriefing
from ariadne.orchestrator import deterministic_decide

logger = logging.getLogger(__name__)


def _build_prompt(briefing: SquadBriefing, issue: Issue, completed_results: list[dict] | None = None) -> str:
    """Build the LLM prompt from briefing + issue + completed member results."""
    roster_lines = []
    for entry in briefing.roster:
        skills = ", ".join(entry.skills) if entry.skills else "none"
        backends = ", ".join(entry.backends) if entry.backends else "none"
        roster_lines.append(
            f"  - agent_id: {entry.agent_id}\n"
            f"    name: {entry.name}\n"
            f"    role: {entry.role}\n"
            f"    skills: {skills}\n"
            f"    backends: {backends}"
        )
    roster_text = "\n".join(roster_lines) if roster_lines else "  (no members)"

    results_section = ""
    if completed_results:
        results_lines = []
        for r in completed_results:
            results_lines.append(f"  - agent: {r.get('agent_name', '?')}, status: {r.get('status', '?')}, output: {str(r.get('result', ''))[:200]}")
        results_section = "\n## Completed Member Results\n" + "\n".join(results_lines) + "\n"

    return f"""{briefing.protocol}

## Squad Roster
{roster_text}

## Squad Instructions
{briefing.instructions}

## Issue
Title: {issue.title}
Description: {issue.description}
{results_section}
## Your Task
Decide which member should handle this issue. Respond with JSON only, no markdown.

To delegate:
{{"target_agent_id": "<agent_id from roster>", "backend": "<backend from roster>", "handoff_prompt": "<what to tell the member>", "reason": "<why this member>", "skill_refs": ["<skill1>"]}}

To mark done (no delegation needed):
{{"delegation": "none"}}
"""


def _parse_response(text: str, briefing: SquadBriefing) -> DelegationDecision | None:
    """Parse LLM JSON response into DelegationDecision or None."""
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        # Try to extract JSON from markdown code blocks
        if "```" in text:
            start = text.find("```")
            end = text.rfind("```")
            if start != end:
                inner = text[start + 3 : end].strip()
                if inner.startswith("json"):
                    inner = inner[4:].strip()
                try:
                    data = json.loads(inner)
                except json.JSONDecodeError:
                    logger.error("failed to parse LLM response as JSON: %s", text[:200])
                    return None
            else:
                return None
        else:
            logger.error("failed to parse LLM response as JSON: %s", text[:200])
            return None

    if data.get("delegation") == "none":
        return None

    # Validate required fields
    required = ["target_agent_id", "backend", "handoff_prompt", "reason", "skill_refs"]
    for field in required:
        if field not in data:
            logger.error("LLM response missing field '%s': %s", field, data)
            return None

    return DelegationDecision(
        target_agent_id=data["target_agent_id"],
        backend=data["backend"],
        handoff_prompt=data["handoff_prompt"],
        reason=data["reason"],
        skill_refs=data["skill_refs"] if isinstance(data["skill_refs"], list) else [],
    )


def make_llm_decide(
    api_key: str | None = None,
    model: str = "deepseek-chat",
    base_url: str = "https://api.deepseek.com/v1",
) -> Callable[..., DelegationDecision | None]:
    """Return a callable (briefing, issue, completed_results=None) -> DelegationDecision | None.

    If api_key is None, falls back to deterministic_decide.
    On API error or 3 consecutive parse failures, falls back to deterministic_decide.
    """
    # Resolve API key from arg or env
    key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        logger.info("no LLM API key — using deterministic_decide fallback")
        return deterministic_decide

    failure_count = [0]

    def decide(briefing: SquadBriefing, issue: Issue, completed_results: list[dict] | None = None) -> DelegationDecision | None:
        prompt = _build_prompt(briefing, issue, completed_results)

        # If 3 consecutive failures, fall back to deterministic
        if failure_count[0] >= 3:
            logger.warning("LLM failed %d times, falling back to deterministic", failure_count[0])
            return deterministic_decide(briefing, issue, completed_results)

        try:
            from openai import OpenAI

            client = OpenAI(api_key=key, base_url=base_url)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a Squad Leader. Respond with JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=500,
            )
            text = response.choices[0].message.content or ""
            result = _parse_response(text, briefing)
            if result is None and "none" not in text.lower():
                # Parse returned None but not explicitly "no delegation" — count as failure
                failure_count[0] += 1
                logger.warning("LLM parse failure #%d", failure_count[0])
                if failure_count[0] >= 3:
                    return deterministic_decide(briefing, issue, completed_results)
                return deterministic_decide(briefing, issue, completed_results)
            failure_count[0] = 0  # reset on success
            return result
        except Exception as e:
            failure_count[0] += 1
            logger.error("LLM API error #%d, falling back: %s", failure_count[0], e)
            return deterministic_decide(briefing, issue, completed_results)

    return decide

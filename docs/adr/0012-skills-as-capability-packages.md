# ADR 0012: Skills as Capability Packages

Date: 2026-07-01

## Status

Accepted.

## Context

Early Ariadne tasks treated skills as labels used for routing. Labels are easy
to display, but they are not enough for execution: a member agent needs the
actual instruction snippet, allowed tools, and verification command that make a
skill operational.

There is also a failure-mode question. If a skill verification command fails
after an agent produced a patch, the runtime can either fail the whole TaskRun
or preserve the result and let the leader re-evaluate with evidence.

## Decision

Persist `Skill` as a first-class capability package:

- routing description
- when-to-use guidance
- prompt snippet
- allowed tools
- verification command
- source path
- version

When a TaskRun is enqueued for an AgentProfile, bound skills are materialized
into the handoff as a `Skill capability package`; `skill_refs` are no longer
only labels.

After successful backend execution, skill verification commands run in the
execution repo path. Their results are recorded as evidence in TaskRun metadata,
`activity_log`, and IssueTimeline `tests_reported` events.

Verification failure is a signal, not a hard gate. The TaskRun result remains
available for leader re-evaluation, retry decisions, or follow-up delegation.

## Consequences

The handoff carries enough context for a member agent to act on a skill without
out-of-band lookup. The system also avoids throwing away useful agent output
when a verifier fails; instead, the leader can use the failure as structured
evidence.

This means "completed" does not always mean "all skill verifications passed."
Consumers must inspect recorded verification evidence when quality gates matter.

## Evidence

- `src/ariadne/store.py` persists skills, binds them to AgentProfiles, and
  composes the `Skill capability package` section.
- `src/ariadne/daemon.py` runs skill verifications after successful backend
  execution and records `verification_passed` / `verification_failed` evidence.
- `src/ariadne/models.py` includes the first-class `Skill` model.
- `tests/test_agent_profile_skill.py` covers materialization and verification
  evidence behavior.

id: polish-001
scope: polish
status: done
depends-on: [eval-001]
```

## Objective

Write the project README with architecture diagram, quickstart, multica
mapping summary, and design decisions. This is the single document a
reviewer/learner reads to understand the project.

## Context

- Doc index: [docs/INDEX.md](../../INDEX.md)
- Architecture: [docs/architecture/README.md](../../architecture/README.md)
- Resume template: multica-python-调查报告.md

## Path

```
README.md
```

## Requirements

### README structure

1. **One-line description** + what it does
2. **Architecture diagram** (ASCII) — 4-layer architecture from docs/architecture/README.md
3. **Quickstart** — install + create agent + create issue + daemon start
4. **Multica mapping** — 3 mechanisms table (mechanism → multica source → what we changed)
5. **Design decisions** — 5 key decisions with one-line rationale
6. **Testing** — how to run tests, test count
7. **Project structure** — file tree with one-line descriptions
8. **Non-goals** — what this project deliberately does NOT do

### Constraints

- README is self-contained — a reader doesn't need to open docs/ to understand the project
- Architecture diagram is ASCII (no images)
- Quickstart commands actually work
- Total README < 200 lines

## Verification

```bash
# Verify README exists and has key sections
grep -c "Architecture" README.md
grep -c "Multica" README.md
grep -c "Quickstart" README.md
```

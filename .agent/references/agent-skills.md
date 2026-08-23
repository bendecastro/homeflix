# Agent Skill Map

Date: 2026-08-23

The bc loop skills are canonical in `~/Sync/Work/PUBLIC/Agents/concepts/<skill>/body/SKILL.md`
and are normally deployed into `~/.agents/skills/<skill>`, `~/.pi/agent/skills/<skill>`, and
`~/.claude/skills/<skill>` via symlink. Do not vendor/copy their bodies into this repo; use this
page as the repo-local map so agents know what to invoke.

## Planning/execution loop

- `/bc-plan-to-issues` — interactive planning front: grill → domain capture → PRD → issue slices.
- `/bc-drain-issues` — AFK executor for `ready-for-agent` issue queues.
- `/improve-codebase-architecture` — optional architecture runway before planning/slicing.

## Supporting skills used by the loop

- Intake/evidence: `/triage`, `/prototype`.
- Planning disciplines: `grilling`, `domain-modeling`, `prd-drafting`, `issue-slicing`, `codebase-design`.
- Source-tree docs: `codebase-docs` — README / existing `docs/` / JSDoc only; not this vault.
- Execution disciplines: `tdd`, `diagnosing-bugs`, `bc-autoresearch-loop`.
- Wiki maintenance: `/bc-wiki-maintain` — compute vault health (link/index/staleness drift,
  unpromoted log backlog, qmd coverage) and promote durable log entries into pages. Additive
  only; contradictions are recorded in `open-questions/`, never resolved for you.

## Vault shape note

This vault is `.agent/`, not `.bc-agent/`. `/bc-wiki-maintain` takes the vault root as an
explicit argument, so pass `.agent` here:

```bash
python3 ~/.agents/skills/bc-wiki-maintain/wiki_lint.py "$(git rev-parse --show-toplevel)/.agent"
```

This vault has no `open-questions/` directory yet. The maintenance pass creates one when it
first has a contradiction to record.

## Agent behavior

If the user's request sounds like planning future codebase work, ask whether to enter
`/bc-plan-to-issues` before implementing. Offer `/improve-codebase-architecture` first when
the main uncertainty is architecture/seams.

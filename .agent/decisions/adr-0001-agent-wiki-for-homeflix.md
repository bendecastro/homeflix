# ADR 0001: Use a project-scoped agent wiki to build homeflix

Date: 2026-06-14

## Status
Accepted

## Context
homeflix (a self-hosted streaming service for Ben's family) is a multi-session build
spanning hardware, storage, media server, acquisition, networking, and deployment —
with several hard-to-reverse decisions. Without a persistent, maintained record, each
agent session re-derives facts (paths, versions, why-we-chose-X) and risks acting on
stale or invented information.

Two patterns were available: Karpathy's LLM-wiki (an LLM-maintained, interlinked
markdown knowledge base) and an existing adaptation of it from an earlier project of
mine, which bends the pattern toward *producing a practical outcome* by adding a
roadmap, plans, ADRs, a live task cursor, and a log.

## Decision
Instantiate a project-scoped agent wiki at `.agent/` in the homeflix repo, modeled
on that earlier adaptation:
- `AGENTS.md` is the maintainer schema (update triggers + session protocol).
- `project/` holds durable architecture, the roadmap (the build spine), and plans.
- `references/`, `conventions/`, `decisions/`, `tasks/`, `templates/` mirror the
  proven layout, domain-tuned for homelab/streaming.
- The wiki is the agent's brain; the homeflix **root** holds actual deliverables
  (compose, configs, scripts).
- No secrets in the wiki; no hardcoded home paths in portable files.

## Consequences
- Every agent session starts from `index.md → AGENTS.md → map.md → tasks/active.md`
  and must keep the wiki live in the same turn as the work.
- The six foundational forks (OS/runtime, filesystem, media server, acquisition shape,
  remote access, media naming) each get their own ADR before they're built on.
- Overhead: discipline to update pages every turn. Accepted because the maintenance
  cost is near-zero for the agent and the payoff is continuity across sessions.
- Rules out scattering homeflix context into chat history or a personal wiki.

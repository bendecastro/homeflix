# homeflix — agent session protocol

Read this file before non-trivial work. Project knowledge lives in ordinary folders at the
repository root so people can browse it; `.bc-agent/` holds agent mechanics only.

## Where things go

Paths are relative to the repository root. Skills look a role up in this table instead of
assuming a path, so change a path here when the project keeps something elsewhere.

| Role | Path | Notes |
| --- | --- | --- |
| work-planned | `work/1-planned/` | agreed work that has not started |
| work-active | `work/2-active/` | work in progress; several items may be active |
| work-done | `work/3-done/` | finished work, always inside a `<year>-<month>/` folder such as `2026-03/` |
| work-dropped | `work/4-dropped/` | abandoned work and rejected requests, with the reason |
| ideas | `work/0-ideas.md` | undecided ideas and observations, one line each |
| decisions | `docs/decisions/` | one numbered decision record per file, named `adr-NNNN-<slug>.md` |
| research | `docs/research/` | one `<date>-<slug>/` folder per investigation |
| glossary | `CONTEXT.md` | canonical names for the project's domain concepts; the project's existing glossary, kept instead of `glossary.md` |
| requirements | `docs/PRD.md` | product-level spec, created on first need; a feature's requirements live in its work item |
| architecture | `ARCHITECTURE.md` | created on first need |
| project-skills | `.agents/skills/` | created on first need; one `local-<name>/` folder per skill, with a README naming shared copies and how to refresh them |
| project-agents | — | not yet decided; ask the owner before creating project-specific agents |
| components | `docs/components/` | verified state of real systems, hosts, tools and services; per-domain pages (hardware, storage, media server, acquisition, networking, deployment); a new service page may start from `templates/service.md` |

When a skill names a default path for one of these roles (for example `docs/adr/`,
`.bc-agent/project/overview.md` or `.bc-agent/out-of-scope/`), use this table's path for that
role instead. Why: shared skills are written for several layouts, and this table is the
project's own answer.

## Work items

A work item is one feature or plan. Template: [templates/work-item.md](templates/work-item.md).

- **The folder is the status.** Move an item between stage folders with `git mv`, in the
  same commit as the work that changes its state. Why: the tree then always shows what is
  planned, active, done and dropped, with nothing else to keep in step.
- **No `Status:` line in an item.** Why: a written status can disagree with the folder.
- **Finished items go into `work/3-done/<year>-<month>/`** (for example `work/3-done/2026-03/`);
  create the month folder when it is missing, and never put an item directly in `3-done/`.
  Why: one rule from the first item on means the folder never needs reorganizing.
- **Dropped work and rejected requests go to `work/4-dropped/`**, with the reason under
  `## Outcome`. Why: a recorded "no, because" stops the same idea being re-argued.
  Asked to delete an item instead, move it anyway and write what you know, even if that is
  only "Dropped <date>; no reason given". Why: a deleted item frees its number for reuse, so
  older references to that number would point at a different item.
- **Names are `<3-digit number>-<slug>`**, for example `012-search-ranking`. The next number
  is the highest number in any stage folder, including the month folders under `3-done/`,
  plus one. Why: a short number is easy to cite and survives a rename of the slug.
- **Only the main checkout creates numbered items.** Working in a git worktree, add a line
  to `work/0-ideas.md` instead. Why: two worktrees cannot see each other's numbers and
  would pick the same one.
- **A small item is one file; a large one is a folder** `NNN-slug/` holding `plan.md` (its
  `## Steps` are the slices) and `requirements.md`, plus any evidence worth keeping. The folder
  moves as a whole. Why: everything about a feature stays in one place through its whole
  life, and the steps already are the slice map, so separate slice files would drift.
- **Several items may be active at once.**
- **GitHub issues are optional.** When one exists, record its number on the item's `Issue:`
  line. Why: the folders are the source of truth; an issue is a pointer to them.
- **Undecided ideas and observations** are one line each in `work/0-ideas.md`. An idea
  becomes a numbered item in `work/1-planned/` once someone decides to do it.
- **Open questions live in work items.** An unanswered question that blocks a piece of work
  is a step in that work item. A standalone question that someone must investigate is a
  planned work item whose answer lands in `docs/research/`. One not worth an item yet is a
  line in `work/0-ideas.md`. Why: `work/` stays the single place that shows what is pending.

## Keep it current

| When you… | Do this |
| --- | --- |
| Start non-trivial work | Read the items in `work/2-active/`, then pick context with [map.md](map.md). |
| Begin agreed work | Create the item from the template, or `git mv` it from `1-planned/` to `2-active/`. |
| Finish a step | Check its box in the item. |
| Finish or drop an item | Fill `## Outcome`, then `git mv` it to its done month folder or to `4-dropped/`. |
| Make an architectural or hard-to-reverse choice | Add a numbered record under `docs/decisions/` from [templates/adr.md](templates/adr.md). |
| Investigate something at length | Write it up in a dated folder under `docs/research/`. |
| Check the state of a real system | Update its page under `docs/components/` from [templates/component.md](templates/component.md). |
| Record a finding | Write it under `docs/research/` from [templates/finding.md](templates/finding.md). |
| Sharpen domain vocabulary | Update `CONTEXT.md`. |
| Learn a recurring command | Add it to [references/commands.md](references/commands.md). |
| Learn a durable fact with no better home | Append one to three lines to [log.md](log.md). |
| Find a page that is wrong | Fix it in the same commit. Never leave a stale claim. |

## Discipline

- Do not invent facts. Mark anything unverified as unverified.
- Inspect `git status` first, stage only your own changes, and commit before finishing.
- Validation commands: [conventions/validation.md](conventions/validation.md). Git and
  publishing: [conventions/git-and-commit-policy.md](conventions/git-and-commit-policy.md).

## Project rules

Carried from the earlier wiki instructions; they still hold.

- **Keep it live.** Update the record in the same turn as the work, not later. A homelab fact
  you remember but do not write down (container versions, drive paths, port mappings, which
  service owns which subdomain, why one tool was chosen over another) is a fact the next agent
  re-derives wrong.
- **Search before reading.** Distill the task to 2–4 keywords and run
  `python3 "$AGENT_CONCEPTS/concepts/bc-wiki-maintain/body/wiki_search.py" --limit 15 "$PWD" <keywords>`;
  open the returned pages and verify the answer in their text. Empty output means reformulate
  once with different keywords. Re-verify anything load-bearing that looks stale (a container
  updated, a drive remounted, an IP changed).
- **Stand up or change a service:** update its page under `docs/components/` (image tag,
  ports, volumes, depends-on, healthcheck status) and log it.
- **Recurring paths and gotchas** go in [references/paths.md](references/paths.md) and
  [references/gotchas.md](references/gotchas.md).
- **Decision records** carry `Date:` and a `## Status` of
  `Proposed | Accepted | Superseded by ADR-XXXX | Rejected`. Reversing one: set the old record
  `Status: Superseded by ADR-XXXX`, link forward, and update dependent pages. The homeflix
  forks that deserve a record: host OS and container runtime, filesystem/RAID strategy, media
  server, acquisition stack shape, remote-access method, and the media naming scheme.
- **Long research** (hardware comparisons, media server choice, indexers) goes in
  `docs/research/`; summarize the durable conclusion into the relevant `docs/components/`
  page and record the choice as a decision. Scratch goes in `.bc-agent/scratch/`.
- **A step fails or is skipped:** say so in the work item and the log. The record reflects
  what happened, not what was hoped.
- **Blocked** (waiting on hardware, a download, a DNS change): record the blocker as a step
  in the work item.
- **No secrets** anywhere in the record: no passwords, API keys, tokens, indexer credentials
  or real `.env` values. Reference where a secret lives, not the secret. See
  [conventions/secrets.md](conventions/secrets.md).
- **No hardcoded absolute or home-directory paths.** The repository is public and must work on
  any host; every deployment path is an `.env` variable.
- **No operator-private detail:** no specific mount paths, disk-encryption status, backup roles,
  LAN addressing or household device inventory. Record the reference build and the reasoning,
  not one person's house.
- **Scope.** The record may hold homeflix architecture and design notes, verified
  hardware/OS/network facts, per-service config notes, decisions, recurring commands, paths and
  gotchas, and media-naming conventions. It must not hold personal (non-homeflix) wiki content,
  secrets, or long raw logs. Keep everything project-local; never push homeflix context into
  the personal wiki.
- **Two doors.** `docs/` (quickstart, first use, hardware, configuration) is for running
  homeflix; nothing in `.bc-agent/` is required reading to deploy.
- **Health check** (periodically, not every turn): broken links, work items in the wrong
  stage folder, decisions that should be superseded, component pages whose image tags or ports
  have drifted from what is running, orphan pages, contradictions. Fix what is safe; note the
  rest in `work/0-ideas.md`.
- **Git:** confirm with the maintainer before pushing.

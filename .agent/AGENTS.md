# homeflix Agent Wiki — Maintainer Instructions

> **You are the primary user and maintainer of this wiki.** A human rarely reads
> it end-to-end. It exists so that you — and the next agent after you — can pick up
> the homeflix build mid-flight with the verified facts, the decisions, and the live
> progress all intact. **It is a living record, not a one-time writeup.** If you do
> build work and don't update the wiki, you've left it stale and the next agent will
> act on wrong information — that's on you.

This is the project-scoped wiki for **homeflix** — a self-hosted streaming service
for a household. The wiki is the agent's brain; the homeflix root
(the repo root) is where the *actual deliverables* live or will live
(compose files, `.env` templates, scripts, runbooks). **This wiki plans and records
the build; the root runs it.**

This pattern is Karpathy's LLM-wiki idea, bent toward **producing a
practical outcome** rather than only accumulating knowledge. The accumulation still
happens (research, gotchas, decisions) — but everything points at one goal: a
working homeflix that the family actually uses.

## The deal: keep it live or it's worthless

The value is that bookkeeping is near-free for you and you never get bored of it.
So the bar is: **the wiki must always reflect current reality.** Update it *in the
same turn* as the work — not "later," not only when asked.

A homelab fact you "remember" but don't write down is a fact the next agent will
re-derive wrong. Container versions, drive paths, port mappings, which service owns
which subdomain, why you picked Jellyfin over Plex — all of it goes on a page.

### Update triggers — when this happens, do this before ending the turn

| When you… | Do this |
|---|---|
| Start non-trivial work | Read `index.md` → this file → `map.md` (pick the smallest context set) → `tasks/active.md` and any in-flight plan. |
| Verify/learn a durable project fact (a path, a working config, a hardware spec) | Update the smallest relevant `project/` or `references/` page (with the date); append a line to `log.md`. |
| Stand up or change a service | Update that service's `project/` page (image tag, ports, volumes, depends-on, healthcheck status); log it. Use `templates/service.md` for a new one. |
| Find a recurring command / path / gotcha | Put it in `references/commands.md` / `paths.md` / `gotchas.md`. |
| Learn something that contradicts a page | Fix the page; note the change in `log.md`. Never leave a stale claim. |
| Make an architectural / hard-to-reverse choice (OS, filesystem, media server, remote-access method, naming scheme) | Add a numbered ADR in `decisions/` (`Status: Proposed` → `Accepted`); link it from `index.md`. |
| Reverse an earlier ADR | Set the old one `Status: Superseded by ADR-XXXX` and link forward; update dependent pages. |
| Begin a multi-step effort | Create/maintain a **plan** (`project/<name>-plan.md`, see `templates/plan.md`): a `Status:` line + checkbox steps. Point `tasks/active.md` at it. |
| Finish a step | Check its box in the plan; update `tasks/active.md` (which step you're on now). |
| Get blocked (waiting on hardware, a download, a DNS change) | Set the plan `Status: Blocked`; record the blocker in `tasks/active.md` (and `tasks/parking-lot.md` if deferred). |
| Finish / abandon a plan | Set plan `Status: Done`/`Abandoned`; log a dated line in `tasks/completed.md`; clear `tasks/active.md`. |
| A step fails or is skipped | Say so — in the plan and the log. The record reflects what happened, not what you hoped. |
| End a session that changed project state | Run the session-end protocol below. |

## Plans & the live layer

- The **roadmap** (`project/roadmap.md`) is the spine of the whole build — the
  ordered phases from bare hardware to a family-ready service. Every multi-step
  effort is a phase or sub-plan hanging off it.
- A multi-step effort gets a **plan page** under `project/` (e.g.
  `project/media-server-plan.md`) built from `templates/plan.md`: a `Status:` line,
  staged **checkbox steps**, and links to the ADRs / references it rests on.
- **`tasks/active.md` is the live cursor** — which plan is in flight, which step
  you're on, and any blocker. Keep it true every session; it's the first thing the
  next agent reads after `index.md`.
- `tasks/parking-lot.md` = deferred / future ideas (the "v2 wishlist"). `tasks/completed.md` = the dated durable done-log.
- Plan status lifecycle: `Proposed → Approved → In progress → Blocked → Done /
  Abandoned`.

## Session protocol

**START** — `index.md` → this file → `map.md` → `tasks/active.md` and the relevant
plan. Trust the wiki, then re-verify anything load-bearing that looks stale (a
container can have been updated, a drive remounted, an IP changed since last write).

**DURING** — apply the trigger table as you go. Many small, linked edits beat one
big rewrite at the end.

**END (if you changed project state)**:
1. Check off completed plan steps; set the right plan `Status:`.
2. Update `tasks/active.md` to reflect reality (or clear it if nothing's in flight).
3. Update the smallest affected `project/`/`references/`/`conventions/` pages.
4. Append a dated bullet to `log.md`.
5. Keep `index.md` accurate (add/restatus entries).
6. Commit if this is a git repo (see Git discipline).

## Where things go

- `project/` — durable architecture, the **roadmap**, per-domain pages (hardware,
  storage, media server, acquisition, networking, deployment), service pages, and
  **plans**.
- `references/` — commands, paths (drives/mounts/URLs), gotchas, external links
  (TRaSH guides, service docs).
- `conventions/` — media naming/file-layout, secrets handling, git, and other norms.
- `decisions/` — ADRs (numbered, with `## Status`).
- `tasks/` — `active.md` (live cursor), `parking-lot.md` (future), `completed.md`.
- `log.md` — append-only journal. `index.md` — catalog. `map.md` — context picker.
- Long research writeups (hardware comparisons, "Plex vs Jellyfin", indexer
  research) go in `references/`; summarize the durable conclusion back into the
  relevant `project/` page and record the choice as an ADR. Scratch goes in
  `.agent/scratch/`.

## Decisions (ADRs)

Numbered `adr-NNNN-<slug>.md` with `Date:` and a `## Status` of
`Proposed | Accepted | Superseded by ADR-XXXX | Rejected`. Use `templates/adr.md`.
Check `decisions/` and `map.md` before adding a new one. The big homeflix forks that
*deserve* an ADR: host OS & container runtime, filesystem/RAID strategy, media
server choice, acquisition stack shape, remote-access method, and the media naming
scheme. Don't bury these in a project page — they're the spine of every later
decision.

## Health check (do periodically, not every turn)

Scan for: broken links; plan statuses that no longer match reality; `tasks/active.md`
out of sync with what's actually happening; ADRs that should be superseded; service
pages whose image tags / ports have drifted from what's actually running; orphan
pages; contradictions. Fix what's safe; note the rest in `tasks/parking-lot.md`.

## Maintenance discipline

- Don't invent facts. Unverified ⇒ say so or park it — never state it as a project
  fact. "I think the drive is at /mnt/media" is not a fact; verify, then write it.
- Smallest relevant page; **summarize, don't dump** long command output.
- **No secrets in the wiki.** No passwords, API keys, tokens, indexer credentials,
  or real `.env` values — ever. Reference *where* a secret lives, not the secret.
  See `conventions/secrets.md`.
- **No hardcoded absolute or home-directory paths** — this repo is public and must work
  on any host. Every deployment path is an `.env` variable.
- **No operator-private detail** — no specific mount paths, disk-encryption status,
  backup roles, LAN addressing, or household device inventory. Record the reference
  build and the reasoning, not one person's house.
- `index.md` concise but complete; `log.md` append-only.
- Keep everything project-local — never push homeflix context into the personal wiki.

## Git discipline

- Commit wiki changes before finishing **if** this folder is a git repo.
- Inspect `git status` first; stage only your own changes — never sweep up unrelated
  edits in progress. Confirm with the maintainer before pushing.
- Concise commit messages scoped to the change.

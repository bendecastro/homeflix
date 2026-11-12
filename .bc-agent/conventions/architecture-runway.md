# Architecture Runway Cadence

Date: 2026-11-12

This page gives a computed, optional signal for when to offer
`/improve-codebase-architecture`. The signal comes from Git history; do not maintain a
counter in this file. The goal is to refresh architecture runway after enough durable project
work has accumulated, not after a calendar interval.

## Computed check

Run this before planning, refactor, or seam-adjacent work. It finds the newest commit whose
message names an architecture review, then counts commits touching the durable project and
live-task surfaces after that review:

```sh
review_tip="$(
  git log --format='%H%x09%s' -- docs/research |
    awk 'tolower($0) ~ /architecture.?review|arch.?review|improve-codebase-architecture/ { print $1; exit }'
)"
if [ -n "$review_tip" ]; then
  review_range="$review_tip..HEAD"
else
  review_range="HEAD"
fi
planning_commits="$(git log --format='%h %cs %s' "$review_range" -- docs work)"
planning_count="$(printf '%s\n' "$planning_commits" | sed '/^$/d' | wc -l)"
printf 'Planning-surface commits since the latest recognized architecture review: %s\n' "$planning_count"
printf '%s\n' "$planning_commits"
```

The snippet uses the default paths. If this project's "Where things go" table in
`.bc-agent/AGENTS.md` names other paths, substitute the research and work paths from the
"Where things go" table for `docs/research` and `docs work` before running it; otherwise
the count misses the project's real planning surfaces.

The `review_tip` lookup is deliberately fail-safe. If no review commit is recognizable, the
range includes all reachable history and the signal may over-count. Git can count commits and
paths; it cannot prove that a change was PRD-sized. Over-counting produces an optional nudge,
not an unsafe edit. If a review commit uses a different subject, the same fallback applies.

## Nudge heuristic

Give a brief, optional nudge when the current request is planning/refactor/seam-adjacent and
one of these is true:

- `planning_count` is 3 or higher. This is a conservative Git-derived proxy for the former
  three-PRD / PRD-sized-slice threshold, not a claim that Git understands PRD semantics.
- The current work shows structural friction: unclear module boundaries, hard-to-test code,
  repeated bugs around the same seam, or "where should this live?" uncertainty.

A request that is itself architecture-heavy can also qualify from its content. This page does
not pretend to compute that judgment from filenames or commit messages.

Do **not** block concrete fixes. The nudge is advisory:

> Optional architecture runway nudge: Git shows multiple planning-surface changes since the last recognized architecture review. Before planning more work, do you want to run `/improve-codebase-architecture` to look for seams and testability improvements?

## Review record

A review has no hand-maintained entry here. Store the review artifact in the dated folder under
`docs/research/` and include `architecture review` in the commit subject so the next computed check
can find its boundary. If the subject does not use that wording, the fail-safe fallback simply
counts more history and may nudge again.

## Agent update rules

- Run the computed check when the request is planning/refactor/seam-adjacent; do not edit this
  page to record the result.
- When `/improve-codebase-architecture` runs, commit its review artifact with an explicit
  `architecture review` subject so future checks can establish a new boundary.
- If `references/agent-skills.md` no longer lists `/improve-codebase-architecture`, remove the
  nudge and explain why here.

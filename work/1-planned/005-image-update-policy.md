# Decide the image update policy

Why: the stack runs `:latest` tags with Watchtower on a family box, so any pull can bring a breaking major release; the choice between auto-update and pinned tags with notify-only has not been made.
Done when: a decision record chooses `:latest` + Watchtower or pinned tags + notify-only, and the compose file and `.env.example` match it.

## Steps

- [ ] Write the decision record.
- [ ] Align `docker-compose.yml` and `.env.example`.

## Outcome

<Filled when the item is finished or dropped.>

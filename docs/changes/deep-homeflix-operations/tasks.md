# Deep Homeflix operations — issue map

Parent: [#3](https://github.com/bendecastro/homeflix/issues/3)

Dependency order only. Completion state lives in GitHub.

1. **Enforce the rendered stack contract end to end** — [#4](https://github.com/bendecastro/homeflix/issues/4)
   - Blocked by: none
   - User stories: 1–7
2. **Make core runtime verification truthful** — [#5](https://github.com/bendecastro/homeflix/issues/5)
   - Blocked by: #4
   - User stories: 19, 20, 23, 30
3. **Reconcile reliable Jellyfin discovery** — [#6](https://github.com/bendecastro/homeflix/issues/6)
   - Blocked by: none
   - User stories: 8–10
4. **Ship fail-closed local backup and scratch restore** — [#7](https://github.com/bendecastro/homeflix/issues/7)
   - Blocked by: none
   - User stories: 24, 26, 27, 29
5. **Add SSH artifact-repository parity and backup compatibility** — [#8](https://github.com/bendecastro/homeflix/issues/8)
   - Blocked by: #7
   - User stories: 25, 28
6. **Establish the secure acquisition VPN gate** — [#9](https://github.com/bendecastro/homeflix/issues/9)
   - Blocked by: #4, #5
   - User stories: 11, 12, 18, 23
7. **Prove and restore VPN fail-closed behavior** — [#10](https://github.com/bendecastro/homeflix/issues/10)
   - Blocked by: #9
   - User stories: 19–23
8. **Deliver the torrent acquisition tracer** — [#11](https://github.com/bendecastro/homeflix/issues/11)
   - Blocked by: #6, #10
   - User stories: 13–18
9. **Add optional Usenet and phase-level acquisition acceptance** — [#12](https://github.com/bendecastro/homeflix/issues/12)
   - Blocked by: #11
   - User stories: 16–18, 30
10. **Complete program-level fixture acceptance and spec handoff** — [#13](https://github.com/bendecastro/homeflix/issues/13)
    - Blocked by: #5, #6, #8, #12
    - User stories: 28–30 and cross-program acceptance

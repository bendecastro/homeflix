# Remote access for off-LAN household members

Why: Homeflix is LAN-only today; off-LAN household members cannot watch. [ADR-0007](../../docs/decisions/adr-0007-remote-access.md) proposes Tailscale first, with Cloudflare Tunnel only for a locked-down TV, and is gated on a device inventory.
Done when: ADR-0007 is accepted or replaced and at least one off-LAN household member signs in and plays.

## Steps

- [ ] Confirm the household device inventory and the TV path.
- [ ] Accept or revise ADR-0007.
- [ ] Implement it (Tailscale on the host, `tailscale serve` for Jellyfin, share the node).

## Outcome

<Filled when the item is finished or dropped.>

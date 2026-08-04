# ADR 0007: Remote access for off-LAN family — Tailscale primary, Cloudflare Tunnel fallback

Date: 2026-06-14

## Status
Proposed   <!-- gated on the family device inventory; see "Decision gate" -->

> Resolves the open fork left by [ADR-0006](adr-0006-traefik-local-remote-access-open.md).
> Once the device inventory is confirmed (`project/overview.md`), this moves to Accepted
> and ADR-0006 follows it to Accepted.

## Context

homeflix is for family who are often off-LAN, but today it's LAN-only (`*.local` via
Traefik, HTTP, no auth on the Traefik dashboard). We need off-LAN access that is
**safe by default** (Ben already runs ProtonVPN and is privacy-conscious — ADR-0005)
without exposing the host to the public internet.

Three viable approaches:

| Approach | Public surface | Works on any device? | TLS | Cost | Notes |
|---|---|---|---|---|---|
| **Tailscale / WireGuard** (mesh VPN) | **None** (outbound only) | No — needs a client app | via Tailscale Serve (`*.ts.net`) | Free (personal) | Safest; the device-support catch is locked-down TVs |
| **Cloudflare Tunnel** | None (outbound `cloudflared`) | **Yes** (plain HTTPS hostname) | Automatic | Free + a domain | ToS discourages heavy video on free plan |
| Port-forward + Traefik + TLS | **Large** (open 443) | Yes | Let's Encrypt | Free | Most exposed; least preferred — rejected |

### The deciding factor: TV clients
- **Tailscale has clients** for iOS/Android/macOS/Windows/Linux, **Apple TV (tvOS 17+)**,
  and **Android TV / Google TV / Fire TV / Nvidia Shield**. Phones, tablets, laptops,
  and those TV platforms are fully covered.
- **Tailscale has NO client** for **Roku, Samsung Tizen, or LG webOS** native TV apps.
  If the family watches on those, a pure-Tailscale setup can't reach them.
- **Cloudflare Tunnel** is just an HTTPS endpoint, so it works on *every* platform incl.
  Roku/Samsung/LG — but Cloudflare's free-plan terms discourage serving large amounts of
  video through their CDN (risk: throttling/termination for heavy streaming).

## Decision

**Primary: Tailscale**, using the **node-sharing** model so it stays free and scales:
- Install Tailscale on the host (Debian); run **`tailscale serve`** to expose Jellyfin
  over HTTPS on the tailnet (real cert, no public exposure).
- Each family member creates their own free personal tailnet; Ben **shares the homeflix
  node** to each. (Node sharing avoids the free-plan user-count limit — they don't become
  users in Ben's tailnet.)
- Admin tools (*arr, Traefik dashboard) stay reachable **only** over the tailnet — never
  public. This also lets us keep `--api.insecure` off the public path.

**For locked-down TVs (Roku / Samsung / LG) — only if they're in the device list:**
1. **Preferred:** add a ~$30–50 **Google TV / Fire TV stick** to that TV so Tailscale runs
   natively and everything stays on one mechanism. Cleanest, no public surface.
2. **Fallback:** stand up **Cloudflare Tunnel** (needs a domain on Cloudflare) for just
   those devices, putting **Jellyseerr + light access behind Cloudflare Access** (email
   OTP / Google login). Keep heavy Jellyfin video on Tailscale where possible to respect
   the free-tier video caveat; accept the ToS risk only for the unavoidable TV streams.

## Decision gate (what flips this to Accepted)

Confirm the family device inventory in `project/overview.md`:
- If all watch devices are phones/tablets/laptops/Apple TV/Android-Fire TV → **Tailscale
  only**; this becomes Accepted as-is and no domain/Cloudflare is needed.
- If any Roku / Samsung / LG native-app TV must be supported → choose stick-swap (stay
  Tailscale-only) vs Cloudflare Tunnel fallback, then Accept.

## Consequences

- **Zero public attack surface** in the Tailscale-only case — strictly better than
  port-forwarding and aligned with the project's privacy posture.
- Each family member installs an app / accepts a node share once — a small onboarding
  step; document it in the family "how to use homeflix" doc (Phase 5).
- The Cloudflare fallback adds a dependency (a domain + Cloudflare account) and a ToS
  caveat for video; scope it to the request portal + the few devices that need it.
- Traefik dashboard hardening (remove/secure `api.insecure`) is still required before any
  Cloudflare-exposed path (carry-over from ADR-0006).
- No change to the LAN path: on-LAN devices keep using `*.local` directly.

## Links
- [ADR-0006](adr-0006-traefik-local-remote-access-open.md) · `project/networking-remote-access.md`
- `project/overview.md` (device inventory) · `project/deployment.md` (Tailscale on host)

# Conventions — Secrets Handling

Updated: 2026-06-14

**No secrets live in this wiki. Ever.** Not in pages, not in the log, not in commits.

## What counts as a secret

Passwords, API keys, indexer logins/cookies, Plex/Jellyfin tokens, Cloudflare/
Tailscale auth keys, reverse-proxy basic-auth, real `.env` values, recovery codes.

## What the wiki *may* record

- *That* a secret exists and *which* service uses it.
- *Where* it lives (e.g. "in the host `.env`", "in <secret manager>").
- The variable name (e.g. `RADARR_API_KEY`) — never its value.

## Where secrets actually live

- **Host `.env`** (gitignored) holds runtime creds: `VPN_USER`, `VPN_PASSWORD`, and
  optional usenet provider / notification / API keys. The repo ships only
  `.env.example` with empty values.
- **Per-service configs** under `${CONFIG_ROOT}/*` hold indexer logins and API keys
  after setup — back these up, never commit or paste them here.

## Host-specific facts

Details of a particular deployment's hardware, encryption status, backup roles, LAN
addressing, and household device inventory are **operator-private** and do not belong in
this wiki even though they aren't strictly secrets. Keep them in a private note outside
the repo. The wiki records the *reference build* and the *design reasoning*, not one
house's configuration.

## Rules

- `.env` is gitignored; only `.env.example` is committed.
- No secret in a URL, screenshot, or pasted log in this wiki.
- If a secret is ever committed or leaked, **rotate it** and note the rotation (not the
  value) in `log.md`. Note that rewriting git history does not un-publish anything that
  was pushed — rotation is the only real remedy.

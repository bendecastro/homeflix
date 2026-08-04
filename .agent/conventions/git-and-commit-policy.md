# Conventions — Git & Commit Policy

Updated: 2026-08-04

This repo is public. Everything committed here is permanent and world-readable.

## Scope

- The whole repo is versioned: deploy artifacts (`docker-compose.yml`, `scripts/`,
  `.env.example`), user docs (`docs/`), and this wiki.
- **Never commit** a real `.env`, service `appdata/`, or media. See `.gitignore`.

## Discipline

- Inspect `git status` before committing; stage only related changes.
- Concise commit messages scoped to the change.
- No secrets in commits or history (`conventions/secrets.md`). If one slips in, **rotate
  it** — history rewriting does not un-publish something already pushed.

## What must not be committed

Beyond secrets, this repo deliberately excludes **operator-private** material: a
specific deployment's mount paths, disk-encryption status, backup roles, LAN addressing,
and household device inventory. Those belong in a private note. The repo describes the
design and the reference build — not one person's house.

## Paths

- Never hardcode absolute or home-directory paths in committed files. Every deployment
  path is an `.env` variable (`DATA_ROOT`, `CONFIG_ROOT`, `CACHE_ROOT`).
- The repo must work identically on any Linux host; assume nothing about the username.

#!/usr/bin/env bash
# homeflix preflight — verify the host is correctly set up before first run.
#
# The defining failure mode of this stack is silent: if downloads and media end up on
# different filesystems, imports still "succeed" — they just copy instead of hardlink,
# waste double the disk, and kill seeding. Nothing appears broken for months.
# This script proves hardlinking works rather than assuming it.
#
# Usage:  ./scripts/preflight.sh

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"

pass=0 warn=0 fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; warn=$((warn+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

hdr "Environment"

if [[ ! -f "$ENV_FILE" ]]; then
  bad ".env not found. Run: cp .env.example .env  (then edit it)"
  printf '\nCannot continue without .env.\n'
  exit 1
fi
ok ".env present"

set -a; # shellcheck disable=SC1090
source "$ENV_FILE"; set +a

for var in DATA_ROOT CONFIG_ROOT CACHE_ROOT; do
  if [[ -z "${!var:-}" ]]; then bad "$var is not set in .env"; else ok "$var=${!var}"; fi
done
for var in VPN_USER VPN_PASSWORD; do
  if [[ -z "${!var:-}" ]]; then
    bad "$var is empty — qBittorrent, NZBGet and Prowlarr will have no network"
  else ok "$var is set"; fi
done
[[ -n "${DOMAIN:-}" ]] && ok "DOMAIN=${DOMAIN}" || warn "DOMAIN unset; defaults to 'local'"

hdr "Docker"
if command -v docker >/dev/null 2>&1; then
  ok "docker: $(docker --version | cut -d, -f1)"
  if docker compose version >/dev/null 2>&1; then
    ok "compose plugin: $(docker compose version --short 2>/dev/null)"
  else
    bad "'docker compose' plugin missing (the old docker-compose v1 is not supported)"
  fi
  docker info >/dev/null 2>&1 && ok "docker daemon reachable" \
    || bad "cannot talk to the docker daemon — is it running, and are you in the 'docker' group?"
else
  bad "docker not installed"
fi

hdr "Paths"
for var in DATA_ROOT CONFIG_ROOT CACHE_ROOT; do
  d="${!var:-}"; [[ -z "$d" ]] && continue
  [[ -d "$d" ]] && ok "$var exists: $d" || bad "$var does not exist: $d"
  case "$d" in
    /home/*|"$HOME"/*) warn "$var is under /home — restrictive home permissions commonly break containers; /opt or /srv is safer" ;;
  esac
done

for sub in torrents usenet media; do
  [[ -d "${DATA_ROOT}/${sub}" ]] && ok "\$DATA_ROOT/${sub} exists" \
    || bad "missing \$DATA_ROOT/${sub} — see docs/quickstart.md"
done

hdr "Hardlink capability (the important one)"
if [[ -d "${DATA_ROOT}/torrents" && -d "${DATA_ROOT}/media" ]]; then
  # -L dereferences: without it, a symlinked media/ reports the symlink's own device
  # and a cross-filesystem layout would pass this check.
  dev_t=$(stat -Lc %d "${DATA_ROOT}/torrents" 2>/dev/null)
  dev_m=$(stat -Lc %d "${DATA_ROOT}/media" 2>/dev/null)
  if [[ "$dev_t" == "$dev_m" ]]; then
    ok "torrents/ and media/ are on the same filesystem"
  else
    bad "torrents/ and media/ are on DIFFERENT filesystems — hardlinks are impossible."
    printf '      Imports would silently fall back to copying: double disk usage, and\n'
    printf '      seeding dies on import. Put both under one filesystem. See ADR-0008.\n'
  fi

  # Prove it, don't infer it.
  src="${DATA_ROOT}/torrents/.homeflix-preflight.$$"
  dst="${DATA_ROOT}/media/.homeflix-preflight.$$"
  if echo homeflix > "$src" 2>/dev/null; then
    if ln "$src" "$dst" 2>/dev/null; then
      i1=$(stat -Lc %i "$src"); i2=$(stat -Lc %i "$dst")
      [[ "$i1" == "$i2" ]] && ok "hardlink smoke test passed (inode $i1 shared)" \
                           || bad "hardlink created but inodes differ — unexpected filesystem behaviour"
      rm -f "$dst"
    else
      bad "could not create a hardlink between torrents/ and media/"
      printf '      The filesystem may not support hardlinks (exFAT/NTFS do not).\n'
    fi
    rm -f "$src"
  else
    bad "cannot write to \$DATA_ROOT/torrents — check ownership and permissions"
  fi
else
  warn "skipped: \$DATA_ROOT/torrents or media/ missing"
fi

hdr "Ownership & permissions"
want_uid="${PUID:-1000}"; want_gid="${PGID:-1000}"
if [[ -d "$DATA_ROOT" ]]; then
  actual_uid=$(stat -Lc %u "$DATA_ROOT"); actual_gid=$(stat -Lc %g "$DATA_ROOT")
  if [[ "$actual_uid" == "$want_uid" ]]; then
    ok "\$DATA_ROOT owned by uid ${want_uid}"
  else
    bad "\$DATA_ROOT is owned by uid ${actual_uid}, but PUID=${want_uid} — imports will fail"
    printf '      Fix: sudo chown -R %s:%s "%s"\n' "$want_uid" "$want_gid" "$DATA_ROOT"
  fi
  [[ -w "$DATA_ROOT" ]] && ok "\$DATA_ROOT is writable by you" || warn "\$DATA_ROOT not writable by $(id -un)"
fi
[[ "$want_uid" == "$(id -u)" ]] || warn "PUID=${want_uid} but you are uid $(id -u) — intentional?"

hdr "Capacity"
if [[ -d "$DATA_ROOT" ]]; then
  avail=$(df -BG --output=avail "$DATA_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')
  if [[ -n "$avail" ]]; then
    (( avail < 50 )) && warn "only ${avail}G free on \$DATA_ROOT" || ok "${avail}G free on \$DATA_ROOT"
  fi
fi

hdr "Compose"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if (cd "$REPO_DIR" && docker compose config --quiet 2>/dev/null); then
    ok "docker-compose.yml is valid and all variables resolve"
  else
    bad "docker compose config failed:"
    (cd "$REPO_DIR" && docker compose config --quiet 2>&1 | sed 's/^/      /' | head -10)
  fi
fi

printf '\n\033[1mResult:\033[0m %d passed, %d warnings, %d failures\n' "$pass" "$warn" "$fail"
if (( fail > 0 )); then
  printf '\033[31mFix the failures above before running docker compose up.\033[0m\n'
  exit 1
fi
(( warn > 0 )) && printf '\033[33mReview the warnings, then: docker compose up -d\033[0m\n' \
               || printf '\033[32mReady. Run: docker compose up -d\033[0m\n'
exit 0

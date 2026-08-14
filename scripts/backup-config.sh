#!/usr/bin/env bash
# shellcheck shell=bash
# homeflix backup — off-box copy of ${CONFIG_ROOT} with consistent SQLite files.
#
# Media is out of scope (large, re-acquirable). This backs up service configs
# and databases only. Destination and retention come from .env — never commit
# a real destination. A backup that has never been restored is not evidence;
# use scripts/restore-config.sh against a scratch directory.
#
# Usage:
#   ./scripts/backup-config.sh                 # run one backup
#   ./scripts/backup-config.sh --install-cron  # daily 03:15 via the invoking user's crontab
#   ./scripts/backup-config.sh -h
#
# Config is data, not shell: this script never `source`s `.env`.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"
INSTALL_CRON=0

usage() {
  cat <<'EOF'
Usage: ./scripts/backup-config.sh [--install-cron] [-h|--help]

Create a dated archive of CONFIG_ROOT and copy it to BACKUP_DEST.

  (default)         Snapshot CONFIG_ROOT, replace live SQLite files with
                    sqlite3 .backup copies, write homeflix-config-<UTC>.tar.gz
                    to BACKUP_DEST, then prune to BACKUP_KEEP archives.
  --install-cron    Add a daily 03:15 crontab line for this script if missing.
  -h, --help        Show this help.

Required in .env: CONFIG_ROOT, BACKUP_DEST.
Optional: BACKUP_KEEP (default 7).

BACKUP_DEST is an rsync destination: user@host:/path or a local directory.
It must not live on the same filesystem as DATA_ROOT (same-disk is not a backup).
Media is not included.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-cron) INSTALL_CRON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# Non-executing dotenv reader (same contract as preflight/#2).
load_env_file() {
  local file="$1" line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

    if [[ "$val" =~ ^\"(.*)\"[[:space:]]*$ ]]; then
      val="${BASH_REMATCH[1]}"
    elif [[ "$val" =~ ^\'(.*)\'[[:space:]]*$ ]]; then
      val="${BASH_REMATCH[1]}"
    else
      if [[ "$val" == *#* ]]; then
        val="${val%%#*}"
      fi
      val="${val%"${val##*[![:space:]]}"}"
    fi
    printf -v "$key" '%s' "$val"
    export "${key?}"
  done < "$file"
}

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

dest_is_remote() {
  local d="$1"
  [[ "$d" == *:* && "$d" != /* && "$d" != .* ]]
}

split_remote() {
  # host:path or user@host:path → REMOTE_SPEC (everything before last :) and REMOTE_PATH
  local d="$1"
  REMOTE_SPEC="${d%:*}"
  REMOTE_PATH="${d##*:}"
}

if [[ ! -f "$ENV_FILE" ]]; then
  die "no .env at $ENV_FILE"
fi
load_env_file "$ENV_FILE"

CONFIG_ROOT="${CONFIG_ROOT:-}"
BACKUP_DEST="${BACKUP_DEST:-}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
DATA_ROOT="${DATA_ROOT:-}"

[[ -n "$CONFIG_ROOT" ]] || die "CONFIG_ROOT is empty"
[[ -n "$BACKUP_DEST" ]] || die "BACKUP_DEST is empty — set an off-box destination in .env"
[[ "$BACKUP_KEEP" =~ ^[1-9][0-9]*$ ]] || die "BACKUP_KEEP must be a positive integer"
[[ -d "$CONFIG_ROOT" ]] || die "CONFIG_ROOT is not a directory"

if [[ -n "$DATA_ROOT" && -d "$DATA_ROOT" ]]; then
  data_dev="$(stat -c '%d' "$DATA_ROOT")"
  dest_check=""
  if ! dest_is_remote "$BACKUP_DEST" && [[ -d "$BACKUP_DEST" ]]; then
    dest_check="$(stat -c '%d' "$BACKUP_DEST")"
  fi
  if [[ -n "$dest_check" && "$dest_check" == "$data_dev" ]]; then
    die "BACKUP_DEST is on the same filesystem as DATA_ROOT — that is not an off-box backup"
  fi
  unset data_dev dest_check
fi

if [[ "$INSTALL_CRON" -eq 1 ]]; then
  cron_line="15 3 * * * /bin/bash ${REPO_DIR}/scripts/backup-config.sh"
  existing="$(crontab -l 2>/dev/null || true)"
  if printf '%s\n' "$existing" | grep -Fqx "$cron_line"; then
    printf 'cron already installed\n'
    exit 0
  fi
  { printf '%s\n' "$existing"; printf '%s\n' "$cron_line"; } | crontab -
  printf 'installed daily 03:15 crontab entry\n'
  exit 0
fi

command -v tar >/dev/null 2>&1 || die "tar is required"
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is required (live DB copy would not be consistent)"
if dest_is_remote "$BACKUP_DEST"; then
  command -v ssh >/dev/null 2>&1 || die "ssh is required for a remote BACKUP_DEST"
  command -v scp >/dev/null 2>&1 || die "scp is required for a remote BACKUP_DEST"
fi

copy_archive() {
  local src="$1" dest="$2"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$src" "${dest%/}/"
  elif dest_is_remote "$dest"; then
    scp -q -B "$src" "${dest%/}/"
  else
    cp -a "$src" "${dest%/}/"
  fi
}

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="homeflix-config-${stamp}.tar.gz"
staging="$(mktemp -d "${TMPDIR:-/tmp}/homeflix-backup.XXXXXX")"
work="$(mktemp -d "${TMPDIR:-/tmp}/homeflix-backup-out.XXXXXX")"
cleanup() {
  rm -rf "$staging" "$work"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap 'cleanup; exit 129' HUP

# Copy everything, then overwrite SQLite files with consistent snapshots and
# drop WAL/SHM leftovers so the archive is a standalone restore.
tar -C "$CONFIG_ROOT" \
  --exclude='*.log' \
  --exclude='*.log.*' \
  --exclude='*/logs/*' \
  -cf - . | tar -C "$staging" -xf -

sqlite_count=0
while IFS= read -r -d '' live; do
  rel="${live#"$CONFIG_ROOT"/}"
  staged="${staging}/${rel}"
  mkdir -p "$(dirname "$staged")"
  if sqlite3 "$live" ".timeout 30000" ".backup '$staged'" >/dev/null 2>&1; then
    sqlite_count=$((sqlite_count + 1))
    rm -f "${staged}-wal" "${staged}-shm"
  fi
done < <(find "$CONFIG_ROOT" -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) -print0)

# WAL/SHM next to any remaining copied DBs (failed .backup left the rsync copy).
find "$staging" -type f \( -name '*-wal' -o -name '*-shm' \) -delete

tar -C "$staging" --numeric-owner -czf "${work}/${archive}" .

prune_local() {
  local dir="$1" keep="$2"
  find "$dir" -maxdepth 1 -type f -name 'homeflix-config-*.tar.gz' -printf '%T@ %p\n' \
    | sort -nr \
    | tail -n +$((keep + 1)) \
    | cut -d' ' -f2- \
    | xargs -r rm -f
}

if dest_is_remote "$BACKUP_DEST"; then
  split_remote "$BACKUP_DEST"
  ssh -o BatchMode=yes "$REMOTE_SPEC" "mkdir -p -- $(printf '%q' "$REMOTE_PATH")"
  copy_archive "${work}/${archive}" "$BACKUP_DEST"
  ssh -o BatchMode=yes "$REMOTE_SPEC" 'sh -s' "$REMOTE_PATH" "$BACKUP_KEEP" <<'EOF'
set -e
cd -- "$1"
find . -maxdepth 1 -type f -name 'homeflix-config-*.tar.gz' -printf '%T@ %p\n' \
  | sort -nr \
  | tail -n +"$(($2 + 1))" \
  | cut -d' ' -f2- \
  | xargs -r rm -f
EOF
else
  mkdir -p -- "$BACKUP_DEST"
  copy_archive "${work}/${archive}" "$BACKUP_DEST"
  prune_local "$BACKUP_DEST" "$BACKUP_KEEP"
fi

printf 'OK archive=%s sqlite=%s keep=%s dest=set\n' "$archive" "$sqlite_count" "$BACKUP_KEEP"

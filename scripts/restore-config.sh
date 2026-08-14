#!/usr/bin/env bash
# shellcheck shell=bash
# homeflix restore — extract a CONFIG_ROOT archive into a scratch directory.
#
# Never writes over the live CONFIG_ROOT. The point of this script is to prove
# a backup opens, not to recover a dead host (that runbook is in
# .agent/project/deployment.md). Media is not in these archives.
#
# Usage:
#   ./scripts/restore-config.sh --list
#   ./scripts/restore-config.sh --to /tmp/homeflix-restore-test
#   ./scripts/restore-config.sh --to /tmp/homeflix-restore-test --archive NAME
#
# Config is data, not shell: this script never `source`s `.env`.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"
LIST_ONLY=0
RESTORE_TO=""
ARCHIVE_NAME=""

usage() {
  cat <<'EOF'
Usage: ./scripts/restore-config.sh [--list] [--to DIR] [--archive NAME] [-h|--help]

Restore a CONFIG_ROOT archive into a scratch directory and check SQLite files.

  --list            List archives at BACKUP_DEST (newest first) and exit.
  --to DIR          Extract here. Refused if DIR is the live CONFIG_ROOT.
  --archive NAME    Archive filename (default: newest homeflix-config-*.tar.gz).
  -h, --help        Show this help.

Required in .env: BACKUP_DEST. CONFIG_ROOT is used only as a refuse-to-clobber guard.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) LIST_ONLY=1; shift ;;
    --to)
      [[ $# -ge 2 ]] || { echo "error: --to needs a directory" >&2; exit 2; }
      RESTORE_TO="$2"
      shift 2
      ;;
    --archive)
      [[ $# -ge 2 ]] || { echo "error: --archive needs a name" >&2; exit 2; }
      ARCHIVE_NAME="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

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
  local d="$1"
  REMOTE_SPEC="${d%:*}"
  REMOTE_PATH="${d##*:}"
}

abs_path() {
  local p="$1"
  (cd "$(dirname -- "$p")" && printf '%s/%s\n' "$(pwd -P)" "$(basename -- "$p")")
}

if [[ ! -f "$ENV_FILE" ]]; then
  die "no .env at $ENV_FILE"
fi
load_env_file "$ENV_FILE"

BACKUP_DEST="${BACKUP_DEST:-}"
CONFIG_ROOT="${CONFIG_ROOT:-}"
[[ -n "$BACKUP_DEST" ]] || die "BACKUP_DEST is empty"

list_archives() {
  if dest_is_remote "$BACKUP_DEST"; then
    split_remote "$BACKUP_DEST"
    ssh -o BatchMode=yes "$REMOTE_SPEC" 'sh -s' "$REMOTE_PATH" <<'EOF'
find "$1" -maxdepth 1 -type f -name 'homeflix-config-*.tar.gz' -printf '%T@ %f\n' \
  | sort -nr \
  | cut -d' ' -f2-
EOF
  else
    find "${BACKUP_DEST}" -maxdepth 1 -type f -name 'homeflix-config-*.tar.gz' -printf '%T@ %f\n' \
      | sort -nr \
      | cut -d' ' -f2-
  fi
}

if [[ "$LIST_ONLY" -eq 1 ]]; then
  list_archives
  exit 0
fi

[[ -n "$RESTORE_TO" ]] || die "pass --to DIR (scratch only; refuses live CONFIG_ROOT)"

if [[ -n "$CONFIG_ROOT" && -e "$RESTORE_TO" && -e "$CONFIG_ROOT" ]]; then
  if [[ "$(abs_path "$RESTORE_TO")" == "$(abs_path "$CONFIG_ROOT")" ]]; then
    die "refusing to restore over live CONFIG_ROOT — pick a scratch directory"
  fi
fi

if [[ -z "$ARCHIVE_NAME" ]]; then
  ARCHIVE_NAME="$(list_archives | head -n 1 || true)"
fi
[[ -n "$ARCHIVE_NAME" ]] || die "no archives found at BACKUP_DEST"
[[ "$ARCHIVE_NAME" == homeflix-config-*.tar.gz ]] || die "archive name must match homeflix-config-*.tar.gz"

mkdir -p -- "$RESTORE_TO"
# Refuse a non-empty dest so we never mix two restores.
if [[ -n "$(ls -A "$RESTORE_TO" 2>/dev/null || true)" ]]; then
  die "--to directory is not empty"
fi

fetch="$(mktemp "${TMPDIR:-/tmp}/homeflix-restore.XXXXXX.tar.gz")"
cleanup() { rm -f "$fetch"; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap 'cleanup; exit 129' HUP

if command -v rsync >/dev/null 2>&1; then
  rsync -a "${BACKUP_DEST%/}/${ARCHIVE_NAME}" "$fetch"
elif dest_is_remote "$BACKUP_DEST"; then
  scp -q -B "${BACKUP_DEST%/}/${ARCHIVE_NAME}" "$fetch"
else
  cp -a "${BACKUP_DEST%/}/${ARCHIVE_NAME}" "$fetch"
fi

tar -C "$RESTORE_TO" -xzf "$fetch"

ok=0
bad=0
while IFS= read -r -d '' db; do
  rel="${db#"$RESTORE_TO"/}"
  if out="$(sqlite3 "$db" "PRAGMA integrity_check;" 2>/dev/null)" && [[ "$out" == "ok" ]]; then
    printf 'OK sqlite %s\n' "$rel"
    ok=$((ok + 1))
  else
    printf 'FAIL sqlite %s\n' "$rel" >&2
    bad=$((bad + 1))
  fi
done < <(find "$RESTORE_TO" -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) -print0)

printf 'OK archive=%s sqlite_ok=%s sqlite_fail=%s dest=%s\n' "$ARCHIVE_NAME" "$ok" "$bad" "$RESTORE_TO"
[[ "$bad" -eq 0 ]]
[[ "$ok" -gt 0 ]]

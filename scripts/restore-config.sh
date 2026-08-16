#!/usr/bin/env bash
# shellcheck shell=bash
# homeflix restore — compatibility adapter around `scripts/homeflix backup`.
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

if [[ "$LIST_ONLY" -eq 1 ]]; then
  exec "$REPO_DIR/scripts/homeflix" backup list
fi

[[ -n "$RESTORE_TO" ]] || { printf 'error: pass --to DIR (scratch only; refuses live CONFIG_ROOT)\n' >&2; exit 1; }

if [[ -n "$ARCHIVE_NAME" ]]; then
  exec "$REPO_DIR/scripts/homeflix" backup restore --to "$RESTORE_TO" --archive "$ARCHIVE_NAME"
fi
exec "$REPO_DIR/scripts/homeflix" backup restore --to "$RESTORE_TO"

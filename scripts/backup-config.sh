#!/usr/bin/env bash
# shellcheck shell=bash
# homeflix backup — compatibility adapter around `scripts/homeflix backup`.
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

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

if [[ "$INSTALL_CRON" -eq 1 ]]; then
  [[ -f "$ENV_FILE" ]] || die "no .env at $ENV_FILE"
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

exec "$REPO_DIR/scripts/homeflix" backup create

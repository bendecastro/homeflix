#!/usr/bin/env bash
# configure-bazarr.sh — first-time (and re-runnable) Bazarr wiring for Homeflix.
#
# Connects Bazarr to Radarr/Sonarr over the Docker network, installs an English
# forced + full language profile as the movie/series default, enables free
# subtitle providers, optionally wires Jellyfin library refresh, and kicks a sync.
#
# Prerequisites:
#   - stack running (`docker compose up -d`) with bazarr + radarr healthy
#   - Radarr (and optionally Sonarr/Jellyfin) already initialized
#   - .env present with CONFIG_ROOT (same as the rest of Homeflix)
#
# Usage:
#   ./scripts/configure-bazarr.sh
#   ./scripts/configure-bazarr.sh --opensubtitles-user U --opensubtitles-password P
#   ./scripts/configure-bazarr.sh --skip-jellyfin
#
# No secrets are written into the repo; keys are read from service config on disk.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"

OPENSUBTITLES_USER=""
OPENSUBTITLES_PASSWORD=""
SKIP_JELLYFIN=0
BAZARR_URL_OVERRIDE=""

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --opensubtitles-user)     OPENSUBTITLES_USER="${2:-}"; shift 2 ;;
    --opensubtitles-password) OPENSUBTITLES_PASSWORD="${2:-}"; shift 2 ;;
    --skip-jellyfin)          SKIP_JELLYFIN=1; shift ;;
    --bazarr-url)             BAZARR_URL_OVERRIDE="${2:-}"; shift 2 ;;
    -h|--help)                usage 0 ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
done

ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; }
die() { bad "$1"; exit 1; }

if [[ ! -f "$ENV_FILE" ]]; then
  die ".env not found at $ENV_FILE — copy .env.example and configure the stack first"
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

[[ -n "${CONFIG_ROOT:-}" ]] || die "CONFIG_ROOT is not set in .env"

xml_apikey() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  # portable: no -P on all sed; use python for robustness if available
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$file" <<'PY'
import sys, xml.etree.ElementTree as ET
print(ET.parse(sys.argv[1]).findtext("ApiKey") or "")
PY
  else
    sed -n 's/.*<ApiKey>\([^<]*\)<\/ApiKey>.*/\1/p' "$file" | head -1
  fi
}

yaml_bazarr_apikey() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$file" <<'PY'
import sys, re
text = open(sys.argv[1], encoding="utf-8").read()
# auth.apikey under auth: block — simple line scan is enough for stock config
m = re.search(r"(?m)^\s*apikey:\s*(\S+)\s*$", text)
# prefer the auth section value: first apikey after ^auth:
auth = re.search(r"(?ms)^auth:\n(.*?)(^[a-z]|\Z)", text)
if auth:
    m2 = re.search(r"(?m)^\s*apikey:\s*(\S+)\s*$", auth.group(1))
    if m2:
        print(m2.group(1).strip("'\""))
        raise SystemExit
if m:
    print(m.group(1).strip("'\""))
PY
  else
    grep -E '^\s*apikey:' "$file" | head -1 | awk '{print $2}' | tr -d \"\'
  fi
}

RADARR_KEY="$(xml_apikey "${CONFIG_ROOT}/radarr/config.xml" || true)"
SONARR_KEY="$(xml_apikey "${CONFIG_ROOT}/sonarr/config.xml" || true)"
BAZARR_KEY="$(yaml_bazarr_apikey "${CONFIG_ROOT}/bazarr/config/config.yaml" || true)"

[[ -n "$RADARR_KEY" ]] || die "Radarr API key not found at ${CONFIG_ROOT}/radarr/config.xml — finish Radarr setup first"
[[ -n "$BAZARR_KEY" ]] || die "Bazarr API key not found — is the bazarr container running and initialized?"

if ! docker ps --format '{{.Names}}' | grep -qx 'bazarr'; then
  die "container 'bazarr' is not running — docker compose up -d bazarr"
fi
if ! docker ps --format '{{.Names}}' | grep -qx 'radarr'; then
  die "container 'radarr' is not running"
fi

# Reach Bazarr via another container on traefik-network (port not published on host).
BAZARR_URL="${BAZARR_URL_OVERRIDE:-http://bazarr:6767}"
CURL_VIA=(docker exec radarr curl -sS)

api() {
  local method="$1" path="$2"; shift 2
  "${CURL_VIA[@]}" -X "$method" -H "X-API-KEY: ${BAZARR_KEY}" "$@" \
    -w "\n%{http_code}" "${BAZARR_URL}${path}"
}

# Jellyfin key (optional): first API key in jellyfin.db if present
JELLYFIN_KEY=""
JELLYFIN_DB="${CONFIG_ROOT}/jellyfin/data/data/jellyfin.db"
if [[ "$SKIP_JELLYFIN" -eq 0 && -f "$JELLYFIN_DB" ]] && command -v python3 >/dev/null 2>&1; then
  JELLYFIN_KEY="$(python3 - "$JELLYFIN_DB" <<'PY' 2>/dev/null || true
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("SELECT AccessToken FROM ApiKeys ORDER BY Id LIMIT 1").fetchone()
print(row[0] if row else "")
PY
)"
fi

echo "Configuring Bazarr…"
ok "Radarr API key present"
[[ -n "$SONARR_KEY" ]] && ok "Sonarr API key present" || bad "Sonarr API key missing — series wiring skipped"
[[ -n "$JELLYFIN_KEY" ]] && ok "Jellyfin API key present" || true

PROVIDERS=(yifysubtitles gestdown bsplayer tvsubtitles supersubtitles embeddedsubtitles)
if [[ -n "$OPENSUBTITLES_USER" && -n "$OPENSUBTITLES_PASSWORD" ]]; then
  PROVIDERS+=(opensubtitlescom)
  ok "OpenSubtitles.com credentials supplied"
fi

# Language profile: English forced + English full (profileId 1)
PROFILES_JSON='[{"profileId":1,"name":"English","cutoff":null,"items":[{"id":1,"language":"en","audio_exclude":"False","audio_only_include":"False","forced":"True","hi":"False"},{"id":2,"language":"en","audio_exclude":"False","audio_only_include":"False","forced":"False","hi":"False"}],"mustContain":[],"mustNotContain":[],"originalFormat":false,"tag":null}]'

# Build form fields for settings POST
FORM_ARGS=()
form() { FORM_ARGS+=(-F "$1"); }

form "languages-enabled=en"
form "languages-profiles=${PROFILES_JSON}"

form "settings-general-use_radarr=true"
form "settings-radarr-ip=radarr"
form "settings-radarr-port=7878"
form "settings-radarr-base_url=/"
form "settings-radarr-ssl=false"
form "settings-radarr-apikey=${RADARR_KEY}"
form "settings-radarr-only_monitored=false"
form "settings-radarr-movies_sync=60"
form "settings-radarr-movies_sync_on_live=true"
form "settings-radarr-full_update=Daily"
form "settings-radarr-use_ffprobe_cache=true"

if [[ -n "$SONARR_KEY" ]]; then
  form "settings-general-use_sonarr=true"
  form "settings-sonarr-ip=sonarr"
  form "settings-sonarr-port=8989"
  form "settings-sonarr-base_url=/"
  form "settings-sonarr-ssl=false"
  form "settings-sonarr-apikey=${SONARR_KEY}"
  form "settings-sonarr-only_monitored=false"
  form "settings-sonarr-series_sync=60"
  form "settings-sonarr-series_sync_on_live=true"
  form "settings-sonarr-full_update=Daily"
  form "settings-sonarr-use_ffprobe_cache=true"
  form "settings-general-serie_default_enabled=true"
  form "settings-general-serie_default_profile=1"
else
  form "settings-general-use_sonarr=false"
fi

form "settings-general-movie_default_enabled=true"
form "settings-general-movie_default_profile=1"

for p in "${PROVIDERS[@]}"; do
  form "settings-general-enabled_providers=${p}"
done

form "settings-general-subfolder=current"
form "settings-general-single_language=false"
form "settings-general-minimum_score=90"
form "settings-general-minimum_score_movie=70"
form "settings-general-use_embedded_subs=true"
form "settings-general-utf8_encode=true"
form "settings-general-upgrade_subs=true"
form "settings-general-days_to_upgrade_subs=7"
form "settings-general-upgrade_manual=true"
form "settings-general-adaptive_searching=true"
form "settings-general-wanted_search_frequency=6"
form "settings-general-wanted_search_frequency_movie=6"
form "settings-general-use_scenename=true"
form "settings-opensubtitlescom-use_hash=true"
form "settings-general-path_mappings="
form "settings-general-path_mappings_movie="

if [[ -n "$OPENSUBTITLES_USER" && -n "$OPENSUBTITLES_PASSWORD" ]]; then
  form "settings-opensubtitlescom-username=${OPENSUBTITLES_USER}"
  form "settings-opensubtitlescom-password=${OPENSUBTITLES_PASSWORD}"
fi

if [[ -n "$JELLYFIN_KEY" ]]; then
  form "settings-general-use_jellyfin=true"
  form "settings-jellyfin-url=http://jellyfin:8096"
  form "settings-jellyfin-apikey=${JELLYFIN_KEY}"
  form "settings-jellyfin-refresh_method=immediate"
  form "settings-jellyfin-update_movie_library=true"
  form "settings-jellyfin-update_series_library=true"

  # Discover library ids from Jellyfin itself (works before Bazarr knows about them)
  LIBS_TMP="$(mktemp)"
  if "${CURL_VIA[@]}" -H "X-Emby-Token: ${JELLYFIN_KEY}" \
      "http://jellyfin:8096/Library/VirtualFolders" >"$LIBS_TMP" 2>/dev/null \
     && command -v python3 >/dev/null 2>&1; then
    while IFS=$'\t' read -r id name type; do
      [[ -z "${id:-}" ]] && continue
      if [[ "$type" == "movies" ]]; then
        form "settings-jellyfin-movie_library_ids=${id}"
        form "settings-jellyfin-movie_library=${name}"
      elif [[ "$type" == "tvshows" ]]; then
        form "settings-jellyfin-series_library_ids=${id}"
        form "settings-jellyfin-series_library=${name}"
      fi
    done < <(python3 - "$LIBS_TMP" <<'PY'
import json, sys
try:
    folders = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit
if not isinstance(folders, list):
    raise SystemExit
for x in folders:
    # ItemId is the library id Bazarr expects; CollectionType is movies/tvshows
    print(f"{x.get('ItemId', '')}\t{x.get('Name', '')}\t{x.get('CollectionType', '')}")
PY
)
  fi
  rm -f "$LIBS_TMP"
  ok "Jellyfin refresh will be enabled"
elif [[ "$SKIP_JELLYFIN" -eq 1 ]]; then
  ok "Skipping Jellyfin (--skip-jellyfin)"
else
  bad "No Jellyfin API key found — skipping Jellyfin wiring (add a key later in the UI)"
fi

RESP="$(api POST /api/system/settings "${FORM_ARGS[@]}")"
CODE="${RESP##*$'\n'}"
BODY="${RESP%$'\n'*}"
if [[ "$CODE" != "204" && "$CODE" != "200" ]]; then
  die "settings POST failed (HTTP ${CODE}): ${BODY}"
fi
ok "Settings saved (HTTP ${CODE})"

# Sync libraries and index existing external subs
for task in update_movies movies_full_scan_subtitles; do
  R="$(api POST "/api/system/tasks?taskid=${task}")"
  C="${R##*$'\n'}"
  [[ "$C" == "204" || "$C" == "200" ]] && ok "Queued task: ${task}" || bad "Task ${task} → HTTP ${C}"
done
if [[ -n "$SONARR_KEY" ]]; then
  for task in update_series series_full_scan_subtitles; do
    R="$(api POST "/api/system/tasks?taskid=${task}")"
    C="${R##*$'\n'}"
    [[ "$C" == "204" || "$C" == "200" ]] && ok "Queued task: ${task}" || bad "Task ${task} → HTTP ${C}"
  done
fi

# Brief status summary
if command -v python3 >/dev/null 2>&1; then
  STATUS_TMP="$(mktemp)"; HEALTH_TMP="$(mktemp)"
  "${CURL_VIA[@]}" -H "X-API-KEY: ${BAZARR_KEY}" "${BAZARR_URL}/api/system/status" >"$STATUS_TMP" 2>/dev/null || true
  "${CURL_VIA[@]}" -H "X-API-KEY: ${BAZARR_KEY}" "${BAZARR_URL}/api/system/health" >"$HEALTH_TMP" 2>/dev/null || true
  python3 - "$STATUS_TMP" "$HEALTH_TMP" <<'PY'
import json, sys
def load(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
st = load(sys.argv[1])
h = load(sys.argv[2])
d = st.get("data") or {}
print(f"  Bazarr {d.get('bazarr_version', '?')} · Radarr {d.get('radarr_version') or '—'} · Sonarr {d.get('sonarr_version') or '—'}")
issues = h.get("data") or []
if issues:
    print("  Health issues:")
    for i in issues:
        print(f"    - {i.get('object')}: {i.get('issue')}")
else:
    print("  Health: no issues")
PY
  rm -f "$STATUS_TMP" "$HEALTH_TMP"
fi

echo
echo "Done. Open http://bazarr.${DOMAIN:-local} — assign the English profile to any"
echo "pre-existing titles via Movies/Series → Mass Edit if they show no profile."
echo "Docs: docs/bazarr.md"

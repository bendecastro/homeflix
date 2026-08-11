# Bazarr first-time setup

Bazarr downloads subtitles for Radarr movies and Sonarr series and writes them
**alongside the media file** so Jellyfin can offer them during playback.

Compose already runs the container. After Radarr (and optionally Sonarr and
Jellyfin) are initialized, finish this wiring once — otherwise the UI is up but
nothing is searched.

Use Homeflix only with content you have the right to access.

## Why this step exists

`docker compose up -d` starts Bazarr with an empty config. Until you connect it to
Radarr/Sonarr, pick a language profile, and enable providers, **no subtitles are
downloaded for new or existing titles**.

The recommended household profile downloads:

1. **English forced** — translations only for foreign-language (or alien) dialogue
2. **English full** — complete dialogue track when you want everything subtitled

## Quick path (script)

With the stack running and Radarr configured:

```bash
./scripts/configure-bazarr.sh
```

The script:

- reads API keys from `$CONFIG_ROOT/{radarr,sonarr,bazarr,jellyfin}` (no keys in Git)
- enables English + the forced/full profile as the default for new movies and series
- connects Radarr and Sonarr over the Docker network (`radarr` / `sonarr`, not localhost)
- enables free providers that work without paid accounts
- optionally wires Jellyfin library refresh when a key and libraries exist
- syncs the library and leaves wanted-search to Bazarr’s scheduler

Optional OpenSubtitles.com (free account, better hash matching):

```bash
./scripts/configure-bazarr.sh \
  --opensubtitles-user 'YOUR_USER' \
  --opensubtitles-password 'YOUR_PASSWORD'
```

Re-run is safe for the defaults it owns; it overwrites the built-in **English**
profile (id `1`) and the provider list it manages.

## Manual path (UI)

Open `http://bazarr.${DOMAIN}` (default `bazarr.local`).

### 1. Languages

**Settings → Languages**

1. Under language filter, enable **English** (add others if your household needs them).
2. **Add New Profile** named `English` with two items, both language English:
   - **Forced (foreign part only)** — for on-screen foreign dialogue only  
   - **Normal or hearing-impaired** — full track  
3. Leave **audio exclude** off for both items so English audio still gets forced
   subs when characters switch language.
4. Enable **default profile for movies** and **for series**, select this profile.
5. Save.

### 2. Providers

**Settings → Providers → +**

Enable several free providers, for example:

- YIFY Subtitles  
- Gestdown  
- BSplayer  
- TVSubtitles  
- SuperSubtitles  
- Embedded Subtitles  

Optional but recommended: **OpenSubtitles.com** with a free account (username +
password). Prefer hash matching when the option is available.

Avoid providers that require a custom user-agent or paid anti-captcha unless you
have configured those.

Save.

### 3. Radarr

**Settings → Radarr**

| Field | Value |
|---|---|
| Enabled | on |
| Hostname | `radarr` |
| Port | `7878` |
| Base URL | empty (or `/` only if you set one in Radarr) |
| API key | from Radarr → Settings → General |
| SSL | off |

**Test**, then save.

**Path mappings:** leave empty when you use the stock Homeflix mounts. Radarr sees
movies under `/data/media/movies` and Bazarr mounts `${DATA_ROOT}/media` at
`/data/media`, so the paths already match. Only add mappings if those roots differ.

### 4. Sonarr

Same pattern: host `sonarr`, port `8989`, Sonarr API key. Test and save. Path
mappings empty under the stock layout.

### 5. Subtitles options

**Settings → Subtitles**

- Subtitle files: **alongside the media file** (not a separate subfolder), so
  Jellyfin picks up `Movie.en.srt` and `Movie.en.forced.srt`
- Leave **single language** off so language codes stay in the filename
- Reasonable defaults: series minimum score **90**, movies **70**

### 6. Jellyfin refresh (recommended)

So new subtitle files appear without a manual library scan:

1. Jellyfin → Dashboard → Advanced → API Keys → create e.g. `Bazarr`
2. Bazarr → Settings → Jellyfin: enable, URL `http://jellyfin:8096`, paste the key
3. Select the Movies and Shows libraries; enable update on those libraries
4. Save

### 7. Existing library

Bazarr applies the default profile to titles **added after** the default is set.
For movies and series already in Radarr/Sonarr:

1. **Movies** (and **Series**) → **Mass Edit**
2. Select all → assign the **English** profile → Save
3. Optionally run **System → Tasks → Sync with Radarr / Sonarr**, then **Search for
   Missing … Subtitles**

## Verify

- **System → Status** shows Radarr (and Sonarr) versions, not blank.
- **System → Providers** lists each provider as **Good** (or throttled briefly after
  first contact — retry later).
- Open a movie: external English and/or English forced tracks appear when found.
- In Jellyfin, the subtitle menu lists **English** and **English - Forced** when both
  files exist next to the video.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Movies list empty | Radarr not enabled, wrong API key, or host set to `localhost` instead of `radarr` |
| “Path mapping” / file not found | Unnecessary or wrong path mappings; remove them under stock Homeflix mounts |
| Providers all empty | Nothing enabled under Settings → Providers |
| Provider ConfigurationError / long throttle | Provider needs credentials or user-agent you did not set — disable it |
| Subs on disk, not in Jellyfin | Wire Jellyfin refresh (above) or run a library scan |
| Only want foreign-language lines | Choose **English - Forced** in the player (or keep only the forced item in the profile) |

More background: [Bazarr wiki](https://wiki.bazarr.media/),  
[TRaSH Bazarr scoring](https://trash-guides.info/Bazarr/Bazarr-suggested-scoring/).

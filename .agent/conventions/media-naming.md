# Conventions — Media Naming & File Layout

Updated: 2026-08-04
Source: prior private design package (see `references/source-research.md`). Decision basis: TRaSH guides.

How media is named so Jellyfin matches metadata and the *arr apps import cleanly.
Decided early (renaming a populated library later is painful).

## Folder + file scheme (under `${DATA_ROOT}/media`)

```
movies/<Movie Title> (Year)/<Movie Title> (Year).mkv
tv/<Series Title>/Season XX/<Series Title> - sXXeXX - <Episode Name>.mkv
music/<Artist>/<Album>/<Track>.flac
```

`archive/{movies,tv,music}` mirrors this for old/rarely-watched content.

## *arr settings that enforce it

In Radarr/Sonarr/Lidarr: **Rename = on**, **Replace Invalid Characters = on**,
**Completed Download Handling = on**, **"Use Hardlinks instead of Copy" = enabled**
([ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md)). The exact
format strings should follow TRaSH recommendations.

## Still to lock

- [ ] Exact Radarr movie format string (quality/codec tags? yes/no).
- [ ] Exact Sonarr series/season/episode format string.
- [ ] Anime / specials / multi-edition handling (if relevant).
- [ ] Bazarr subtitle naming + language tags.

## Why it matters

Jellyfin scrapes metadata by matching these names against TMDb/TVDb/MusicBrainz. Bad
naming → wrong posters, missing episodes, duplicates. Consistency here is what makes
the family-facing library look "real."

## Links
- [Storage](../project/storage.md) · [Media server](../project/media-server.md) · [TRaSH](../references/external-links.md)

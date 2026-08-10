# First use

The stack is running and wired together. This guide covers the last mile: creating
household accounts, connecting a Jellyfin client, making a request, and understanding
what happens next.

Use Homeflix only with content you have the right to access.

## 1. Confirm the household URLs

Homeflix uses local names by default:

- Jellyfin (watch): `http://jellyfin.local`
- Jellyseerr (request): `http://jellyseerr.local`
- qBittorrent (administrator only): `http://qbittorrent.local`

Replace `local` if you changed `DOMAIN` in `.env`. These names work only after you
configure LAN DNS as described in the [quickstart](quickstart.md#7-lan-dns).

Jellyfin also works without LAN DNS at:

```text
http://<homeflix-host-ip>:8096
```

Keep the administration interfaces on a trusted LAN. Do not expose Radarr, Sonarr,
Prowlarr, qBittorrent, or the Traefik dashboard directly to the internet.

## 2. Create household accounts

In Jellyfin, sign in as the administrator and open **Dashboard → Users**. Create a
non-administrator account for each household member who will watch or request media.
Use library and parental-access controls where appropriate.

Jellyseerr authenticates household members through Jellyfin. After creating an account,
have the person sign in to Jellyseerr once and confirm they can submit a request. Review
request permissions in Jellyseerr if requests require administrator approval or should
have limits.

## 3. Connect a playback device

Install a Jellyfin client on the TV, streaming box, phone, tablet, or computer. Enter one
of these server addresses:

```text
http://jellyfin.local
http://<homeflix-host-ip>:8096
```

Sign in with a non-administrator Jellyfin account. Prefer **direct play** where possible;
transcoding consumes more host resources and should be tested separately if you rely on
it.

## 4. Make imports appear promptly

Jellyfin's real-time folder monitoring may not notice every containerized import. Wire the
*arr applications to Jellyfin so they request a refresh when an import completes:

1. In Jellyfin, open **Dashboard → Advanced → API Keys**, create a dedicated key such as
   `Radarr and Sonarr`, and copy it. Treat the key as a secret; keep it in service
   configuration and never commit it.
2. In Radarr, open **Settings → Connect**, add **Emby / Jellyfin**, and set:
   - Host: `jellyfin`
   - Port: `8096`
   - Use SSL: off
   - API key: the dedicated Jellyfin key
   - Send notifications: off
   - Update library: on
   - Events: **On Download**, **On Upgrade**, and **On Rename**
3. In Sonarr, add the same connection, but enable **On Import Complete** and **On Rename**.
   Import Complete refreshes once after a batch instead of once per episode in a season
   pack.
4. Run each connection's built-in **Test**, then save only after it succeeds.

The internal host is `jellyfin`, not `localhost`: Radarr, Sonarr, and Jellyfin are separate
containers on the same Docker network.

## 5. Make a useful first request

For the first test, request a small title that is already released and that your
configured indexers can find. An upcoming movie is still a valid request, but it will not
start downloading merely because it was accepted.

The normal path is:

1. Jellyseerr accepts the request.
2. Radarr (movie) or Sonarr (show) monitors the title and searches indexers.
3. qBittorrent or NZBGet downloads a matching release.
4. Radarr or Sonarr imports and renames it under `/data/media`.
5. Radarr or Sonarr asks Jellyfin to refresh the library, making the title available to
   clients.

Typical request states:

- **Requested / approved:** the request reached the appropriate *arr application.
- **Processing:** a release may be downloading or waiting to import.
- **Available:** Jellyfin has matched the imported media.
- **Requested for an unreleased title:** Radarr or Sonarr keeps monitoring it and searches
  again when releases become available. No qBittorrent job is expected yet, and you do
  not need to request it again.

A released request can also remain pending when no configured indexer returns a release
that satisfies the selected quality profile. Check Radarr or Sonarr before assuming the
request pipeline is broken.

## 6. Follow a request as an administrator

Check the pipeline in this order:

1. **Jellyseerr:** open the request and confirm it was approved and sent to Radarr or
   Sonarr.
2. **Radarr/Sonarr:** open **Activity → Queue** and the title's history. Confirm whether a
   release was found, rejected, downloading, or imported.
3. **qBittorrent:** check progress at `http://qbittorrent.local`, or use the host port if
   LAN DNS is unavailable:

   ```text
   http://<homeflix-host-ip>:<QBITTORRENT_PORT>
   ```

   The initial username is `admin`. Find the container-generated temporary password with:

   ```bash
   docker compose logs qbittorrent 2>&1 | sed -n 's/.*session: //p' | tail -1
   ```

   Change it under **Tools → Options → Web UI**.
4. **Jellyfin:** check the relevant library. If an imported title is missing, retest the
   *arr application's **Emby / Jellyfin** connection, then run **Scan Library** to recover
   the current item while diagnosing the event-driven refresh.

If a download never starts, inspect Gluetun first:

```bash
docker compose ps
docker compose logs --tail 100 gluetun
```

The VPN kill switch intentionally prevents the download and indexer services from using
the network when the tunnel is unhealthy.

## 7. Verify the first import

Play the imported title from a real household device. Then verify that the import used a
hardlink rather than consuming a second copy of the file:

```bash
find "$DATA_ROOT/media" -links 1 -type f
```

For torrent imports, files reported by this command have only one link and need
investigation. See [Verify hardlinks end to end](quickstart.md#9-verify-hardlinks-end-to-end)
for the inode-level check.

## First-use checklist

- [ ] A non-administrator Jellyfin account can sign in.
- [ ] A real playback device can reach Jellyfin and play a title.
- [ ] Radarr and Sonarr both pass their **Emby / Jellyfin** connection tests.
- [ ] A released Jellyseerr request reaches Radarr or Sonarr.
- [ ] The download client receives and completes the job.
- [ ] The title appears in Jellyfin after import.
- [ ] A torrent import has a hardlink count of at least 2.
- [ ] The qBittorrent temporary password has been replaced.

Homeflix is LAN-only until you deliberately configure remote access. Do not solve remote
playback by forwarding the administration ports from your router.

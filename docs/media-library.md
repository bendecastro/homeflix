# Adding titles to the library

Core setup configures Radarr and Sonarr; it never acquires anything. Adding titles is a
separate, explicitly requested step. This page covers the supported way to do it in bulk and
the *arr API behaviours that make the naive approach quietly wrong.

If you only want to add the occasional film or show, use Jellyseerr and stop reading — it
handles all of this for you. This page is for scripted or agent-driven bulk adds.

## The client

`scripts/homeflix_setup/api/library.py` provides `LibraryClient`, which adds titles to an
already-configured Radarr or Sonarr instance:

```python
from homeflix_setup.api import LibraryClient, read_api_key

key = read_api_key(config_root, "sonarr", expected_uid=os.getuid())
client = LibraryClient("sonarr", "http://127.0.0.1:8989", key)

client.add_series(393189, [1, 2], quality_profile_id=6, root_folder_path="/data/media/tv")
```

It is idempotent: a title already in the library is reported as `present` and no write is
issued. Quality profile ids and root folder paths are not guessed — discover them from
`/api/v3/qualityprofile` and `/api/v3/rootfolder`, or reuse what `ArrClient` selected during
setup.

## Four behaviours worth knowing

These are properties of the *arr APIs, not of any particular deployment. Each one is covered
by a regression test in `tests/test_api_library.py`.

### 1. Sonarr reverts season monitoring you set immediately after adding

Adding a series with `addOptions.monitor` and then correcting `seasons[].monitored` with a
`PUT` appears to work — the read straight after the `PUT` returns exactly what you wrote.
It is then silently reverted, because Sonarr applies the add options from a **refresh task
that finishes after the POST returns**. The series ends up unmonitored and nothing logs an
error.

A single read immediately after a write is not evidence. `LibraryClient` re-asserts the
monitoring and requires the desired state to be observed **twice across a delay** before
trusting it, and only then issues search commands. If it never stabilises the add fails with
`monitoring_unstable` rather than leaving a half-configured series behind.

Any script that sets season monitoring needs this settle-and-confirm loop, whatever language
it is written in.

### 2. `series/lookup` can rank an unrelated exact-title match first

Searching a series by name can return a completely different show whose title matches
exactly, ahead of the one you meant — including cases where the intended series is
disambiguated in its own title (`Some Show` versus `Some Show (2025)`).

Resolve titles to a TVDB/TMDB id once, by hand or with a reviewed lookup, then pin every
subsequent call to that id. `LibraryClient` only accepts external ids and verifies that the
lookup response carries the id it asked for; a mismatch raises `lookup_mismatch` instead of
adding the wrong title. Always dry-run a bulk add and read the resolved titles before
applying it.

### 3. A season-pack download shows one queue record per episode

A ten-episode season pack produces ten queue rows with identical release names. They are not
duplicate grabs. Group `/api/v3/queue` records by `downloadId` before concluding that
anything was grabbed twice.

### 4. Radarr and Sonarr are not published on the host

The Compose stack exposes the proxy, Jellyfin and Jellyseerr; Radarr and Sonarr are reachable
only inside the Docker network or through the proxy. `curl` against the host's loopback port
returns nothing at all, which reads like a hung service rather than a closed port.

Reach them through the container, for example:

```bash
docker exec sonarr curl -s -H "X-Api-Key: $KEY" http://localhost:8989/api/v3/series
```

Two related traps when scripting that:

- The API key lives in `${CONFIG_ROOT}/<service>/config.xml` as `<ApiKey>`. Read it with
  `read_api_key()`, which validates ownership and permissions first.
- The LinuxServer images ship BusyBox `grep`, which has no `-P`. Extract the key with
  `sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p'` if you must do it in shell.

Send the key as an `X-Api-Key` **header**. Passing `?apikey=` puts a credential into every
container log and shell history.

## Verifying an add

Adding a title only queues work. Confirm the chain actually completed:

```bash
# monitored seasons are what you asked for
docker exec sonarr curl -s -H "X-Api-Key: $KEY" http://localhost:8989/api/v3/series

# unique downloads behind the queue, not raw record count
docker exec sonarr curl -s -H "X-Api-Key: $KEY" "http://localhost:8989/api/v3/queue?pageSize=200"
```

Once files import, check that hardlinking held — imported media should share an inode with
the download, so link count is 2 or more:

```bash
find "${DATA_ROOT}/media" -type f -links 1
```

Any primary media file with a link count of 1 was copied rather than hardlinked, which means
the single `${DATA_ROOT}:/data` mount has been split somewhere. See
[configuration.md](configuration.md).

<p align="center">
  <img src="assets/logo-wordmark.svg" alt="Websharr" width="520">
</p>

A bridge between **Webshare.cz** (premium account) and the ***arr stack** (Sonarr/Radarr). A single container that acts as:

- a **Newznab indexer** (`/torznab/api`) — translates Sonarr/Radarr queries into Webshare searches and returns the results as a release feed,
- a **SABnzbd download client** (`/sabnzbd/api`) — accepts a grab, downloads the file through your premium account into a folder, and lets *arr import it as usual.

<p align="center">
  <img src="assets/flow.svg" alt="Request in Jellyseerr → Sonarr/Radarr search Websharr's Newznab indexer (direct or via Prowlarr) → grab → Websharr's SABnzbd client downloads from Webshare → *arr imports into the library → Jellyfin. Websharr looks Czech titles up in TMDB." width="820">
</p>

## Getting started

```bash
docker compose up -d --build
```

Then open **`http://localhost:9797/ui`** and walk through the first-run setup:
create your Websharr account and enter your Webshare.cz **premium** login
(just a username/e-mail and password — Webshare.cz has no API key). Websharr
generates its own API key for Sonarr/Radarr; you'll find it in **Settings**.

Environment variables (`.env`, all optional) can pre-fill the Webshare login
and pin the API key — values saved in the UI take precedence and persist in
`/config/settings.json`.

In `docker-compose.yml`, adjust the `/downloads` volume so it points to the same folder that Sonarr/Radarr sees (otherwise set up a Remote Path Mapping).

## Web UI

A monitoring and testing interface runs at **`http://localhost:9797/ui`**
(the bare domain redirects here; sign-in required, Sonarr-style):

- **Queue** — live progress with **pause / resume** and delete per download.
- **History** — completed/failed downloads with retry and clear.
- **Search** — manual search whose **Grab** button pushes a release through the
  same SABnzbd flow Sonarr uses; results show container, resolution and length.
  When a grabbed file has no `SxxEyy`/year in its name, a small form asks for the
  series/season/episode (or title/year) so the import parses.
- **Log** — live backend activity, including the Sonarr/Radarr HTTP requests, so
  you can watch the integration from the browser.
- **Help** — the flow diagram and the Sonarr/Radarr setup steps below, in-app.
- **Settings** — Webshare account, API key, password.

For TV searches Websharr normalizes release names to `Series SxxEyy - …` so
Sonarr can parse and import even the many CZ files that ship without `SxxEyy`
in the filename. Some manual imports are still occasionally needed.

## Setup in Sonarr / Radarr

### Indexer (Settings → Indexers → Add → Newznab, Custom)

Add it as **Newznab**, not Torznab. Newznab is the usenet protocol, so Sonarr/Radarr
route grabs to the paired SABnzbd download client below. A Torznab indexer is treated
as torrent and its grabs would be sent to a torrent client instead.

| Field | Value |
|---|---|
| URL | `http://websharr:9797/torznab` |
| API Path | `/api` |
| API Key | copy from Websharr → Settings |
| Categories | 5000 (TV) / 2000 (Movies) |

### Download client (Settings → Download Clients → Add → SABnzbd)

| Field | Value |
|---|---|
| Host | `websharr` |
| Port | `9797` |
| URL Base | `/sabnzbd` |
| API Key | copy from Websharr → Settings |
| Category | `tv` (Sonarr) / `movies` (Radarr) |

Host names like `websharr` work when everything shares a Docker network; otherwise
use the LAN IP and port. Also works through **Prowlarr** — add it there as a
**Generic Newznab** indexer (same URL/API path/key) and let Prowlarr sync it to
Sonarr/Radarr; the SABnzbd download client is still added directly in each *arr.

## Good to know

- **Shared download folder.** Websharr and Sonarr/Radarr must see the *same*
  finished-download folder, or import can't find the files. Mount the same host
  path into all of them, or set a Remote Path Mapping in *arr. Websharr reports
  paths under its `COMPLETE_DIR`.
- **Czech content and quality profiles.** Many CZ releases have no resolution in
  the filename; Websharr reads the real height from Webshare and labels them
  (`1080p`, …) so quality is detected. But a profile that blocks non-English
  (e.g. a *Not English* custom format) will still reject Czech releases — allow
  Czech (or use a dedicated profile) if you grab CZ content.
- Watch the **Log** tab to see the *arr requests arrive in real time.

## Czech titles (TMDB)

Sonarr/Radarr search by the title they know — usually the English one — but
Webshare files are almost always named in Czech (`bez.vedomi…`, not
`the.sleepers…`), so those searches find nothing. Websharr bridges that gap by
looking the request up in **TMDB**:

- Paste a **TMDB API Read Access Token** (v4 auth, from your themoviedb.org
  account) into **Settings → Czech titles (TMDB)**, or pre-fill it with the
  `TMDB_TOKEN` environment variable (the UI value wins).
- For each query Websharr finds the title in TMDB and searches Webshare under its
  **original title** (`original_name` / `original_title`) — that's where the
  Czech name lives — while keeping the canonical title for the release name so
  *arr shows and imports it correctly (e.g. it grabs `Bez vědomí` but reports
  `The Sleepers`). ID-based lookups are tried first, then a name match.
- **Search aliases** (Settings) are a manual override: map a title to the exact
  Webshare name yourself for the cases where TMDB has no Czech title or picks the
  wrong match.

The token is optional — without it, aliases still work and everything else runs
as normal; you just lose the automatic Czech-title resolution.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WEBSHARE_USERNAME` / `WEBSHARE_PASSWORD` | — | Webshare.cz credentials (initial default; editable in the UI) |
| `WEBSHARR_API_KEY` | auto-generated | API key for the Newznab/SABnzbd endpoints; set to pin a fixed value |
| `TMDB_TOKEN` | — | TMDB API Read Access Token for [Czech-title](#czech-titles-tmdb) lookups (UI value wins) |
| `SETTINGS_FILE` | `/config/settings.json` | persisted settings (UI account, Webshare login, API key) |
| `COMPLETE_DIR` | `/downloads/complete` | finished downloads (`<cat>/<name>/file`) |
| `INCOMPLETE_DIR` | `/downloads/incomplete` | in-progress files |
| `STATE_FILE` | `/config/state.json` | queue/history persistence |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | concurrent downloads |
| `SEARCH_LIMIT` | `60` | max results from Webshare per query |

## How search results are cleaned up

Webshare's fulltext is loose (OR-based) and matches on any token, so raw results
are noisy. For a TV query Websharr:

- tries `S01E05`, `1x05` and a bare `05` (common for CZ uploads) variants;
- keeps only files whose name **starts with the show title** (drops unrelated
  files that merely contain a shared word or the episode number);
- keeps only the **requested episode** (read from the file name), so a search
  for E05 doesn't return E01–E08;
- ranks by relevance, and labels quality from the real video resolution.

Even so, because matching is filename-based, the occasional odd result slips
through — use Interactive Search / the UI when it does.

## Limitations

- Webshare searches **by file names only** — no metadata; result quality depends
  on how files are named. When *arr sends a TVDB/IMDb/TMDB ID directly, Websharr
  resolves it through TMDB (see [Czech titles](#czech-titles-tmdb)); note that
  Prowlarr converts IDs to a text query before forwarding, so behind Prowlarr the
  ID is unavailable and the TMDB name match is used instead.
- Interrupted downloads (restart, network outage, or **pause**) resume where they
  left off via an HTTP Range request; a failed job can be restarted via SABnzbd
  `mode=retry` (Retry in the UI History).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/
.venv/bin/uvicorn app.main:app --reload --port 9797
```

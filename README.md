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
docker compose up -d
```

This pulls the prebuilt image `ghcr.io/janprochy/websharr` (tags: `latest`,
the version number, e.g. `0.3.11`). To build from source instead, uncomment
`build: .` in `docker-compose.yml` and run `docker compose up -d --build`.

Then open **`http://localhost:9797/ui`** and walk through the first-run setup:
create your Websharr account and enter your Webshare.cz **premium** login
(just a username/e-mail and password — Webshare.cz has no API key). Websharr
generates its own API key for Sonarr/Radarr; you'll find it in **Settings**.

Environment variables (`.env`, all optional) can pre-fill the Webshare login
and pin the API key — values saved in the UI take precedence and persist in
`/config/settings.json`.

Set `DOWNLOADS_DIR` (in `.env`, or as a stack environment variable in Portainer)
to the same host folder that Sonarr/Radarr see as their download directory
(otherwise set up a Remote Path Mapping). `CONFIG_DIR` and `WEBSHARR_PORT` work
the same way; see `.env.example` for the defaults.

## Web UI

A monitoring and testing interface runs at **`http://localhost:9797/ui`**
(the bare domain redirects here; sign-in required, Sonarr-style):

<p align="center">
  <img src="assets/screenshot-settings.png" alt="Websharr settings screen with Osaka Jade theme, Webshare account status, API key controls and download settings" width="920">
</p>

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
  Czech name of a Czech show lives — while keeping the canonical title for the
  release name so *arr shows and imports it correctly (e.g. it grabs `Bez
  vědomí` but reports `The Sleepers`). ID-based lookups are tried first, then a
  name match.
- For foreign titles Websharr additionally searches the **Czech alternative
  titles** from TMDB — the name a Czech dub is filed under (e.g. `Kačeří
  příběhy` for *DuckTales*), which is not the original title.
- **Search aliases** (Settings) are a manual override: map a title to the exact
  Webshare name yourself for the cases where TMDB has no Czech title or picks the
  wrong match.

The token is optional — without it, aliases still work and everything else runs
as normal; you just lose the automatic Czech-title resolution.

### Runtime check (optional, off by default)

With the token, ID-based searches (what Sonarr/Radarr send via Prowlarr) can also
**check each file's duration** against the TMDB runtime of the movie or episode
and drop files far off it — an hour-long documentary that happens to start with
a short show's Czech name, a 20-minute special named like the feature, a
5-minute excerpt. Turn it on in **Settings → Czech titles (TMDB)** or with
`RUNTIME_CHECK=true` (the UI value wins); off, searches behave exactly as
without it (no runtime lookups, no extra probes).

- Only a mismatch counts: a file of the right length is never taken as proof
  that it is the right title.
- Movies pass from 0.6× to 2× the runtime; a named edition (*Extended*,
  *Director's Cut*, *Uncut*, *Special Edition*, …) may run any amount longer.
- Episodes pass from 0.5× to 2.6× the episode's own runtime; multi-episode
  files (`S01E01E02`, `S01E01-E03`) may run longer. When TMDB has only the
  show's typical episode length (season searches, episodes without a runtime),
  only files far shorter than it (< 0.35×) are dropped. Specials (season 0) are
  not checked.
- Files of unknown length always pass.
- Dropped files are replaced from up to two further Webshare result pages, so
  *arr still gets up to the limit it asked for when more good files exist.
- The check gets 30 s per search: whatever isn't measured by then goes out
  unchecked rather than holding the search up (Sonarr gives an indexer 100 s).

### Movie title prefix (optional, off by default)

A movie file named only in Czech (`Pribeh hracek 1080p CZ.mkv`) doesn't parse
to the movie in Radarr. Matched only by the echoed id, its grab is then held for
a **manual import** ("matched to movie by ID, Manual Import required"). With
this option an ID-based movie search puts the TMDB title and year in front —
`Toy Story 1995 - Pribeh hracek 1080p CZ` — so Radarr maps the release by title
and imports it by itself. That removes Radarr's safety net, so the prefix is
added only when **all** of these hold (otherwise the name goes out unchanged
and Radarr asks for a manual import, as without the option):

- The name, without its extension, **begins with the TMDB title** or one of the
  film's TMDB original/Czech titles, followed by nothing but the year, language
  markers (`CZ`, `dabing`, `titulky`, …) and then a technical tag (resolution,
  source, codec, audio) with the release details after it. No other word
  (`Rocky Returns`), no number or Roman numeral (`Rocky 2`, `Rocky V`,
  `Rocky CZ 2` — a language marker doesn't hide what follows it) before the
  tags, and no stray number, part, CD or sample marker anywhere in the name.
- **Every year** in the name is the TMDB year, exactly.
- A numbered title (`Rocky II`) must carry its number (`II` or `2`).
- The file's **length is known** (Webshare file info) and within 0.85–1.25× of
  the TMDB runtime. Unknown length or runtime means no prefix, and a matching
  length is never enough on its own.
- TMDB was asked which other films the name could be — another film of the same
  title (search, English and Czech) or a part of the movie's collection whose
  title extends it (`Rocky` → `Rocky II`). If there is one, only a name with
  the year gets the prefix; if one has the same year too, or the name also
  carries the words such a part adds, none does. Only the first page of each
  search is looked at. If TMDB can't answer (error, or 15 s per search), no
  prefix.

A name that already starts with `<title> <year>` is left as it is. Turn it on in
**Settings → Czech titles (TMDB)** or with `MOVIE_TITLE_PREFIX=true` (the UI
value wins); it needs the TMDB token. Off, release names are exactly as without
it and nothing extra is looked up. It works with or without the runtime check.
The prefixed titles are checked against a Radarr-like title/year parser in the
tests; that is not a live Radarr import test.

## Monitoring and notifications

- **Dashboard widget.** `GET /stats?apikey=…` returns compact JSON (active/queued
  counts, speed, failed count, Webshare VIP days) for a Homepage `customapi`
  widget or similar.
- **Prometheus.** `GET /metrics?apikey=…` exposes gauges
  (`websharr_queue_downloading`, `websharr_download_speed_bytes`,
  `websharr_history_failed`, `websharr_vip_days`, …) for Grafana.
- **Notifications.** Add [Apprise](https://github.com/caronc/apprise) URLs
  (Discord, Telegram, ntfy, Slack, Gotify, …) in **Settings → Notifications** or
  via `NOTIFY_URLS`. Websharr alerts on a failed download, when Webshare is
  unreachable, and when your VIP is about to expire (threshold `NOTIFY_VIP_DAYS`).

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WEBSHARE_USERNAME` / `WEBSHARE_PASSWORD` | — | Webshare.cz credentials (initial default; editable in the UI) |
| `WEBSHARR_API_KEY` | auto-generated | API key for the Newznab/SABnzbd endpoints; set to pin a fixed value |
| `TMDB_TOKEN` | — | TMDB API Read Access Token for [Czech-title](#czech-titles-tmdb) lookups (UI value wins) |
| `RUNTIME_CHECK` | `false` | `true` drops files far off the TMDB runtime ([runtime check](#runtime-check-optional-off-by-default); UI value wins) |
| `MOVIE_TITLE_PREFIX` | `false` | `true` puts the TMDB title and year in front of unambiguous movie names ([movie title prefix](#movie-title-prefix-optional-off-by-default); UI value wins) |
| `NOTIFY_URLS` | — | Apprise URLs (comma-separated) for failure/VIP notifications (UI value wins) |
| `NOTIFY_VIP_DAYS` | `7` | Warn when Webshare VIP has this many days left or fewer |
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

<p align="center">
  <img src="assets/logo-wordmark.svg" alt="Websharr" width="520">
</p>

A bridge between **Webshare.cz** (premium account) and the ***arr stack** (Sonarr/Radarr). A single container that acts as:

- a **Torznab indexer** (`/torznab/api`) — translates Sonarr/Radarr queries into Webshare searches and returns the results as a release feed,
- a **SABnzbd download client** (`/sabnzbd/api`) — accepts a grab, downloads the file through your premium account into a folder, and lets *arr import it as usual.

Flow: request in Jellyseerr → Sonarr/Radarr searches via Websharr → grab → Websharr downloads from Webshare → *arr imports and renames into your library.

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
(sign-in required, Sonarr-style) — live download queue with progress, history
with retry, a manual search tab whose **Grab** button pushes a release through
the same SABnzbd flow Sonarr would use, and a settings page for the Webshare
account, the API key and your password.

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

Also works through Prowlarr as a **Generic Newznab** indexer.

### Download client (Settings → Download Clients → Add → SABnzbd)

| Field | Value |
|---|---|
| Host | `websharr` |
| Port | `9797` |
| URL Base | `/sabnzbd` |
| API Key | copy from Websharr → Settings |
| Category | `tv` (Sonarr) / `movies` (Radarr) |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WEBSHARE_USERNAME` / `WEBSHARE_PASSWORD` | — | Webshare.cz credentials (initial default; editable in the UI) |
| `WEBSHARR_API_KEY` | auto-generated | API key for the Torznab/SABnzbd endpoints; set to pin a fixed value |
| `SETTINGS_FILE` | `/config/settings.json` | persisted settings (UI account, Webshare login, API key) |
| `COMPLETE_DIR` | `/downloads/complete` | finished downloads (`<cat>/<name>/file`) |
| `INCOMPLETE_DIR` | `/downloads/incomplete` | in-progress files |
| `STATE_FILE` | `/config/state.json` | queue/history persistence |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | concurrent downloads |
| `SEARCH_LIMIT` | `60` | max results from Webshare per query |

## Limitations

- Webshare searches **by file names only** — no metadata. Websharr tries both `S01E05` and `1x05` variants, filters to video files, and sorts by size, but result quality ultimately depends on how files on Webshare are named. Occasional manual intervention (Interactive Search) is needed.
- Searching by IMDb/TVDB ID is not possible (caps doesn't advertise it, so *arr sends text queries).

Interrupted downloads (container restart, network outage) resume where they left off — Websharr continues a partially downloaded file with an HTTP Range request; a failed job can be restarted via SABnzbd `mode=retry`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/
.venv/bin/uvicorn app.main:app --reload --port 9797
```

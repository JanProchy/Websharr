<p align="center">
  <img src="assets/logo-wordmark.svg" alt="Websharr" width="520">
</p>

A bridge between **Webshare.cz** (premium account) and the ***arr stack** (Sonarr/Radarr). A single container that acts as:

- a **Torznab indexer** (`/torznab/api`) — translates Sonarr/Radarr queries into Webshare searches and returns the results as a release feed,
- a **SABnzbd download client** (`/sabnzbd/api`) — accepts a grab, downloads the file through your premium account into a folder, and lets *arr import it as usual.

Flow: request in Jellyseerr → Sonarr/Radarr searches via Websharr → grab → Websharr downloads from Webshare → *arr imports and renames into your library.

## Getting started

```bash
cp .env.example .env   # fill in your Webshare login
docker compose up -d --build
```

`.env`:

```env
WEBSHARE_USERNAME=your-login
WEBSHARE_PASSWORD=your-password
WEBSHARR_API_KEY=some-random-string
```

In `docker-compose.yml`, adjust the `/downloads` volume so it points to the same folder that Sonarr/Radarr sees (otherwise set up a Remote Path Mapping).

## Web UI

A monitoring and testing interface runs at **`http://localhost:9797/ui`** — live download queue with progress, history with retry, and a manual search tab whose **Grab** button pushes a release through the same SABnzbd flow Sonarr would use. No configuration needed; it shares `WEBSHARR_API_KEY` with the API endpoints.

## Setup in Sonarr / Radarr

### Indexer (Settings → Indexers → Add → Torznab, Custom)

| Field | Value |
|---|---|
| URL | `http://websharr:9797/torznab` |
| API Path | `/api` |
| API Key | the value of `WEBSHARR_API_KEY` |
| Categories | 5000 (TV) / 2000 (Movies) |

Also works through Prowlarr as a **Generic Torznab** indexer.

### Download client (Settings → Download Clients → Add → SABnzbd)

| Field | Value |
|---|---|
| Host | `websharr` |
| Port | `9797` |
| URL Base | `/sabnzbd` |
| API Key | the value of `WEBSHARR_API_KEY` |
| Category | `tv` (Sonarr) / `movies` (Radarr) |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WEBSHARE_USERNAME` / `WEBSHARE_PASSWORD` | — | Webshare.cz credentials |
| `WEBSHARR_API_KEY` | `websharr` | API key for both the Torznab and SABnzbd endpoints |
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

# garmin-sync-hub

Garmin Connect automations in one self-hosted app, with one Garmin login:

- **Liftosaur → Garmin**: turns your Liftosaur workouts into Garmin strength activities, with exercises, sets, reps and weights mapped to Garmin's exercise catalog, calories, and heart rate merged from your watch.
- **Zwift maps**: adds the Zwift map to the activity your watch recorded during a Zwift session, so you keep the watch's full data (heart rate, footpod, running dynamics, Training Effect) *and* get the map.

It runs as one Docker container with a web UI:

- **Overview**: one card per source (Garmin, Liftosaur, Zwift) with its state, what is pending and a quick action, plus the latest activity. The Garmin card checks the connection every 30 minutes (or on **Check now**) and warns a week before the Garmin sign-in expires.
- **Activity**: everything sent to Garmin, from every source, with tabs to filter by source or show only what needs your attention (failures, pairs waiting to be merged).
- **Liftosaur** and **Zwift**: one section per source, with its own pages.
- **Settings**: one tab per source.

## Install (Docker compose)

```sh
mkdir garmin-sync-hub && cd garmin-sync-hub
curl -O https://raw.githubusercontent.com/Jeffs24/garmin-sync-hub/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/Jeffs24/garmin-sync-hub/main/.env.example
docker compose up -d
```

Open `http://SERVER:8127`. The setup page asks for:

1. **Garmin Connect** (required): email and password, plus the verification code if your account uses MFA. Only the tokens are stored, in `data/garminconnect/`.
2. **Liftosaur API key** (optional): only needed for the Liftosaur module. In Liftosaur: Settings → API Keys.
3. Your profile (weight, birth year, sex), used for calorie estimation in Liftosaur FIT files.

To update: `docker compose pull && docker compose up -d`. The image `:1` follows every 1.x release; pin an exact version (`:1.0.0`) to update only when you choose.

Set `HUB_PASSWORD` in `.env` to protect the web UI with a password if it can be reached from outside your network.

## Liftosaur → Garmin

**Liftosaur → Workouts** lists your Liftosaur workouts; sync them one by one or all at once (**Sync now**, also on the Overview). Auto-sync runs on a schedule you choose on the same page.

A synced workout has three actions:

- **Undo** (workouts uploaded as a new activity only): deletes the Garmin activity this app created and makes the workout pending again. A watch activity that was updated with your sets is never deleted.
- **Re-sync** (after an edit in Liftosaur): uploads the workout again. An uploaded activity is replaced; an updated watch activity gets its sets written again.
- **Unsync**: forgets the sync without touching Garmin.

- **Mappings**: Liftosaur exercises are mapped to Garmin's exercise categories. Unmapped exercises show as "Unknown" on Garmin; map them from **Liftosaur → Mappings**.
- **Heart rate**: when your watch recorded the session, its heart rate is merged into the uploaded workout.
- **Update existing activities**: if your watch already recorded the strength session, the workout's exercises and sets are written into that activity instead of creating a new one.

Command line (inside the container: `docker compose exec garmin-sync-hub garmin-sync-hub <command>`): `sync`, `status`, `list`, `unmapped`, `map`, `unsync`, `init` (interactive setup), `serve` (web UI), `export-garmin-token`.

## Zwift maps

During a Zwift session, a footpod or sensors paired with the watch give the watch activity all the data but no map. Zwift sends its own activity to Garmin with the map but poor data.

For each pair of *watch activity without a map* + *Zwift activity with a map* that overlap in time:

1. Both original FIT files are downloaded and backed up in `data/zwift_backups/<id>/`.
2. The Zwift GPS position (and, optionally, altitude) is added to the watch FIT, point by point, by timestamp. Everything else in the watch file is kept byte for byte. The only other change is the file creation time, one second later, because Garmin refuses a file it has already seen, even after the activity was deleted. The device identity is untouched, so the activity still counts as a watch activity.
3. The merged file is read back and validated.
4. **Only when you ask** (Merge, Merge all, or `ZWIFT_AUTO_MERGE=true`), the merged file is imported first; the original watch activity and the Zwift copy are deleted only once the merged activity is on Garmin. Name, description, type and gear are restored.

Every Garmin step is checked against the activity list, since Garmin sometimes answers with an error (such as a 504) for a request that went through. If an earlier attempt left the watch activity deleted without a replacement, clicking **Merge** again imports it from the backups.

**Zwift → Pairs** lists the pairs found, with a filter by status. Actions update the row in place, and a pair being merged refreshes itself until it is done. Its **Auto-sync** switch turns the scheduled scan on or off and sets how often it runs; the other Zwift options (merge mode, period scanned, matching thresholds) are in **Settings → Zwift** and apply right away. **Simulate** builds the merged file without touching Garmin, and **Preview** shows it as Garmin will get it: map, stats and charts. **Restore** undoes a merge from the backups: the original watch activity is re-imported first (name, description, type and gear restored), then the merged activity is deleted, and a Zwift copy removed by the merge is brought back. The restored pair stays out of automatic merges. A **How it works** card on the same page shows what the merge takes from each activity, with your current settings.

### Zwift account and history

Connect your Zwift account in **Settings → Zwift**, then click **Sync history** in **Zwift → History and files** from a date: the app lists your Zwift activities through Zwift's unofficial Companion API and downloads the FIT file of each one that overlaps a watch activity without a map. Once connected, new Zwift activities are also fetched at each scan. Only the Zwift tokens are stored, in `data/zwift_tokens.json`.

Without a Zwift account, you can upload Zwift FIT files by hand: from zwift.com (activity → Download .fit) or from your Strava data export (`activities/` folder, `.fit.gz` accepted).

Offline merge of two files: `garmin-sync-hub zwift-merge-files watch.fit zwift.fit -o merged.fit`.

## Configuration (`.env`)

The `ZWIFT_*` variables give the starting values; once you save the Zwift settings in the web UI, the saved values win.

| Variable | Default | Purpose |
|---|---|---|
| `HUB_PASSWORD` | *(empty)* | password for the web UI |
| `LIFTOSAUR_API_KEY` | *(empty)* | Liftosaur API key (also settable in the UI) |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | *(empty)* | automatic Garmin login at startup (accounts without MFA) |
| `ZWIFT_AUTO_MERGE` | `false` | merge every detected pair without a click |
| `ZWIFT_DELETE_SOURCE` | `true` | delete the Zwift copy on Garmin after merging |
| `ZWIFT_COPY_ALTITUDE` | `false` | also copy the Zwift virtual altitude |
| `ZWIFT_SCAN_DAYS` | `7` | period scanned for new pairs |
| `ZWIFT_AUTO_SCAN` | `true` | scan on a schedule (Auto-sync) |
| `ZWIFT_SCAN_INTERVAL_MINUTES` | `30` | time between two scans |
| `ZWIFT_MIN_OVERLAP` | `0.5` | minimum overlap (shared time / combined time) |
| `ZWIFT_MIN_POSITION_RATIO` | `0.5` | minimum share of watch points that get a position |
| `ZWIFT_TIME_TOLERANCE_S` | `2` | maximum gap between a watch point and a Zwift point |
| `ZWIFT_SOURCE_NAMES` | `zwift` | how the Zwift copy is recognized (manufacturer or name) |
| `ZWIFT_SOURCE_MANUFACTURERS` | `260` | FIT manufacturer required for the source (260 = Zwift) |
| `ZWIFT_EMAIL` / `ZWIFT_PASSWORD` | *(empty)* | automatic Zwift login at startup |
| `ZWIFT_AUTO_SYNC` | `true` | fetch new Zwift activities at each scan |

## Data

Everything lives in the `data/` volume (`DATA_DIR`):

| Path | Content |
|---|---|
| `garminconnect/garmin_tokens.json` | Garmin tokens |
| `liftosaur.json` | Liftosaur settings (API key, profile, timing, auto-sync) |
| `zwift_settings.json` | Zwift settings saved from the web UI |
| `liftosaur.db` | synced workouts, sync log, custom mappings |
| `zwift.db` | Zwift pairs and their status |
| `zwift_tokens.json` | Zwift tokens |
| `zwift_backups/` | original and merged FIT files |

## Good to know

- **Automatic retries**: with auto-sync on (Liftosaur) or auto-merge and auto-sync on (Zwift), a failed workout or merge is tried again after 30 minutes, then 2 hours, then 8 hours. After that it waits for a click. The Activity page shows when the next try is due.
- **Health check**: `GET /healthz` answers without sign-in (`{"status": "ok", "version": ..., "garmin": ...}`) and is used by the Docker `HEALTHCHECK`. A Garmin problem is reported there but does not mark the container unhealthy.

- The tool uses the unofficial Garmin Connect API (through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)) and the unofficial Zwift Companion API. Both can change without notice.
- A merged Zwift activity gets a new Garmin ID: links to the old one stop working.

## Development

```sh
pip install -e .[dev]
pytest
```

## Credits

garmin-sync-hub reuses code from these projects:

| Project | Author | License | What comes from it |
|---|---|---|---|
| [drkostas/hevy2garmin](https://github.com/drkostas/hevy2garmin) | Konstantinos Georgiou | MIT | the original Garmin sync pipeline: strength FIT generation, exercise mapping to Garmin's catalog, heart rate merge, calorie estimation, web UI and CLI |
| [Zettt/liftosaur2garmin](https://github.com/Zettt/liftosaur2garmin) | Andreas Zeitler | MIT | the port of hevy2garmin to Liftosaur: Liftosaur API client, Liftosaur exercise names, workout normalization |

Libraries and services used as is:

- [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) (MIT): Garmin Connect login and API calls.
- [FastAPI](https://fastapi.tiangolo.com), [Jinja2](https://jinja.palletsprojects.com), [htmx](https://htmx.org) and [Leaflet](https://leafletjs.com) for the web UI; map tiles © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
- The Liftosaur REST API ([liftosaur.com/doc/api](https://www.liftosaur.com/doc/api)).

garmin-sync-hub is released under the MIT license (see [LICENSE](LICENSE)).

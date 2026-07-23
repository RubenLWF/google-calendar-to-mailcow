# Google Calendar -> SOGo/mailcow calendar sync

Periodically copies each board member's Google Calendar into their
SOGo (mailcow) calendar. Runs as a single always-on Docker container that syncs
everyone every 15 minutes.

## How it works

For each person (`people/<name>.env`), every cycle:

1. **`filter_gcal_ics.py`** downloads their Google "Secret iCal address" and
   drops events older than the cutoff, writing a compact `<name>.ics`. It pins
   each event's `DTSTAMP` to `LAST-MODIFIED`, so an unchanged event
   produces byte-identical output run-to-run.
2. **`sync_all.py`** generates a vdirsyncer config and runs `vdirsyncer sync`
   into that person's SOGo CalDAV calendar.

Because the filter output is deterministic, vdirsyncer's `status/` cache skips
unchanged events and only writes real changes. Moves/edits propagate (their
`LAST-MODIFIED`/`DTSTART` change); deletions and events aging past the cutoff
disappear from the `.ics` and are removed from SOGo.

## Deploy (Docker)

```bash
docker compose up -d --build      # build image + start the loop
docker compose logs -f            # watch it sync
```

The container loops forever (`SYNC_INTERVAL=900`s) and restarts unless stopped.

### Volumes

| Mount | Purpose |
|-------|---------|
| `./people` → `/config/people` (ro) | one `<name>.env` per board member |
| `gcal-data` → `/data` | vdirsyncer `status/` cache + filtered `.ics` |

If `/data` (the `status/` cache) is lost, the next run re-syncs all board member's calendars
from scratch. Keep it on the named volume.

## Add / remove a board member

Adding a person = drop in one file. No code or config edits.

```bash
cp people/EXAMPLE.env people/alice.env
$EDITOR people/alice.env          # fill GCAL_ICS_URL + SOGO_URL/USERNAME/PASSWORD
docker compose restart            # or just wait for the next cycle
```

The filename (without `.env`) is the person id. `EXAMPLE.env` is ignored
(Use function names for these, since they are unique).
A malformed/incomplete env file is logged and skipped — it never blocks others.

## Configuration (env vars)

All shared settings live in one place: the `environment:` block of
`docker-compose.yml`. A person's `people/<name>.env` can override any of the
date-window vars for just that person.

| Var | Default | Meaning |
|-----|---------|---------|
| `SYNC_INTERVAL` | `900` | seconds between full cycles (0 = run once and exit) |
| `TZ` | - | timezone for the cutoff-date logic |
| `GCAL_CUTOFF_DATE` | - | start of the sync **window** (fixed `YYYY-MM-DD`, **required**); keep events ending on/after it |
| `GCAL_CUTOFF_END_DATE` | - | end of the window (fixed `YYYY-MM-DD`); keep events beginning on/before it. |

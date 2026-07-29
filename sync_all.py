"""Fetch + filter each person's Google calendar and sync it into their SOGo/mailcow mailbox.

One env file per person lives in $PEOPLE_DIR (see people/example.env). For each
person this script:
  1. loads their env file and runs filter_gcal_ics.py -> $FILTERED_DIR/<name>.ics
  2. generates the vdirsyncer config with one pair per person
  3. discovers and syncs that person's pair into SOGo

Each person is synced independently; one broken feed or bad credential is logged and skipped so it never blocks the others.

Configuration:
  PEOPLE_DIR      dir of <name>.env files      (default: <script>/people)
  STATUS_DIR      vdirsyncer status/cache dir  (default: <script>/status)  <- persist me!
  FILTERED_DIR    per-person filtered .ics     (default: <script>/filtered)
  CONFIG_PATH     generated vdirsyncer config  (default: <script>/config)
  VDIRSYNCER_BIN  vdirsyncer executable        (default: "vdirsyncer" on PATH)
  PYTHON_BIN      python for the filter        (default: this interpreter)
  SYNC_INTERVAL   loop every N seconds         (default: 900)
"""

import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import dotenv_values

log = logging.getLogger("sync_all")

SCRIPT_DIR = Path(__file__).resolve().parent


def _path(env_name: str, default: Path) -> Path:
    return Path(os.environ.get(env_name, str(default)))


PEOPLE_DIR = _path("PEOPLE_DIR", SCRIPT_DIR / "people")
STATUS_DIR = _path("STATUS_DIR", SCRIPT_DIR / "status")
FILTERED_DIR = _path("FILTERED_DIR", SCRIPT_DIR / "filtered")
CONFIG = _path("CONFIG_PATH", SCRIPT_DIR / "config")
FILTER = SCRIPT_DIR / "filter_gcal_ics.py"

PYTHON = os.environ.get("PYTHON_BIN", sys.executable)
VDIRSYNCER = os.environ.get("VDIRSYNCER_BIN", "vdirsyncer")

SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", 900))

REQUIRED = ("GCAL_ICS_URL", "SOGO_URL", "SOGO_USERNAME", "SOGO_PASSWORD")

# Shared defaults read from the process environment. Can be overridden in a person's own env file.
DEFAULT_KEYS = ("GCAL_CUTOFF_DATE", "GCAL_CUTOFF_END_DATE")


def read_env(path: Path) -> dict[str, str]:
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def load_people() -> list[tuple[str, dict[str, str]]]:
    """Return [(name, env), ...] for each valid person env file.

    Shared defaults are read from the process environment, set them
    in docker-compose `environment:`. Each person's own file overrides them.
    """
    defaults = {k: os.environ[k] for k in DEFAULT_KEYS if k in os.environ}
    people = []
    for envfile in sorted(PEOPLE_DIR.glob("*.env")):
        name = envfile.stem
        # Skip example.env
        if name == "example":
            continue
        env = {**defaults, **read_env(envfile)}
        missing = [k for k in REQUIRED if not env.get(k)]
        if missing:
            log.error("skipping %s: env is missing %s", name, ", ".join(missing))
            continue
        people.append((name, env))
    return people


def write_config(people: list[tuple[str, dict[str, str]]]) -> None:
    """Generate the vdirsyncer config."""
    blocks = [
        "[general]",
        f'status_path = "{STATUS_DIR}"',
        "",
    ]
    for name, env in people:
        ics = FILTERED_DIR / f"{name}.ics"
        blocks.append(
            f"""[pair google_{name}]
a = "google_{name}_src"
b = "sogo_{name}"
collections = null
conflict_resolution = "a wins"

[storage google_{name}_src]
type = "singlefile"
path = "{ics}"
read_only = true

[storage sogo_{name}]
type = "caldav"
url = "{env['SOGO_URL']}"
username = "{env['SOGO_USERNAME']}"
password = "{env['SOGO_PASSWORD']}"
"""
        )
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(blocks))
    os.chmod(CONFIG, 0o600)


def filter_calendar(name: str, env: dict[str, str]) -> None:
    child = os.environ.copy()
    child.update(env)
    child["GCAL_ICS_OUT"] = str(FILTERED_DIR / f"{name}.ics")
    subprocess.run([PYTHON, str(FILTER)], env=child, check=True)


def _target_marker(name: str) -> Path:
    """Sidecar file recording the SOGo URL this person last synced to."""
    return STATUS_DIR / f"google_{name}.synced_url"


def reset_status(name: str) -> None:
    """Delete vdirsyncer's status for one pair.

    Used when a person's SOGo target changes: the old status lists items from
    the previous collection, which vdirsyncer reads as a mass deletion against
    the new (empty) collection and refuses to sync. Clearing it makes the next
    run a clean first sync. Only this person's status is touched.
    """
    pair = f"google_{name}"
    targets = list(STATUS_DIR.glob(f"{pair}.*"))
    pair_dir = STATUS_DIR / pair
    if pair_dir.exists():
        targets.append(pair_dir)
    for p in targets:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


def reset_status_if_target_changed(name: str, url: str) -> None:
    marker = _target_marker(name)
    if marker.is_file() and marker.read_text().strip() != url:
        log.info("SOGo target for %s changed; clearing stale status", name)
        reset_status(name)


def record_target(name: str, url: str) -> None:
    _target_marker(name).write_text(url)


def sync_pair(name: str) -> None:
    pair = f"google_{name}"
    # Always discover before sync
    subprocess.run([VDIRSYNCER, "-c", str(CONFIG), "discover", pair], check=True)
    subprocess.run([VDIRSYNCER, "-c", str(CONFIG), "sync", pair], check=True)


def run_cycle() -> None:
    for d in (STATUS_DIR, FILTERED_DIR):
        d.mkdir(parents=True, exist_ok=True)

    people = load_people()
    if not people:
        log.warning("no valid people configured in %s", PEOPLE_DIR)
        return

    write_config(people)

    ok = 0
    for name, env in people:
        try:
            log.info("syncing %s", name)
            reset_status_if_target_changed(name, env["SOGO_URL"])
            filter_calendar(name, env)
            sync_pair(name)
            record_target(name, env["SOGO_URL"])
            ok += 1
        except subprocess.TimeoutExpired:
            log.error("timeout syncing %s (>%ss); skipping", name, SYNC_TIMEOUT)
        except Exception as exc:
            log.error("failed syncing %s: %s", name, exc)
    log.info("cycle complete: %d/%d people synced", ok, len(people))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    log.info("syncing every %ds", SYNC_INTERVAL)
    while True:
        try:
            run_cycle()
        except Exception:
            log.exception("run_cycle crashed; continuing")
        next_sync = datetime.now() + timedelta(seconds=SYNC_INTERVAL)
        log.info("next sync at %s", next_sync.strftime("%Y-%m-%d %H:%M:%S"))
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())

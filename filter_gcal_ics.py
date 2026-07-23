#!/usr/bin/env python3
"""Filter a Google Calendar ICS feed to a [start, end] date window.

Keeps events that overlap the window, i.e. events that both:
  - end on/after the start cutoff (lower bound), and
  - begin on/before the end cutoff (upper bound), if one is set.

The recurrence structure is preserved (no expansion into individual events),
so the output stays compact and SoGo still sees one item per series.

Writes a single .ics that vdirsyncer reads via a `singlefile` storage.

Config via environment variables:
  GCAL_ICS_URL          the Google "Secret address in iCal format"
  GCAL_ICS_OUT          output path
  GCAL_CUTOFF_DATE      fixed start cutoff YYYY-MM-DD
  GCAL_CUTOFF_END_DATE  fixed end cutoff YYYY-MM-DD
"""

import os
import sys
import datetime as dt
from urllib.request import urlopen, Request

from icalendar import Calendar
from dateutil.rrule import rrulestr

FEED_URL = os.environ["GCAL_ICS_URL"]
OUTPUT_PATH = os.environ["GCAL_ICS_OUT"]
CUTOFF_DATE_ENV = dt.date.fromisoformat(os.environ["GCAL_CUTOFF_DATE"])
CUTOFF_END_DATE_ENV = dt.date.fromisoformat(os.environ["GCAL_CUTOFF_END_DATE"])


def to_date(value):
    """Normalize a date or datetime to a plain date (in UTC) for comparison."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc)
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def as_datetime(value):
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    return None


def event_start_date(comp):
    """Start date of a single VEVENT component."""
    dtstart = comp.get("DTSTART")
    if dtstart is None:
        return None
    return to_date(dtstart.dt)


def event_end_date(comp):
    """End date of a single VEVENT component."""
    dtend = comp.get("DTEND")
    if dtend is not None:
        return to_date(dtend.dt)
    dtstart = comp.get("DTSTART")
    if dtstart is None:
        return None
    start = dtstart.dt
    duration = comp.get("DURATION")
    if duration is not None and isinstance(start, dt.datetime):
        return to_date(start + duration.dt)
    return to_date(start)


def latest_occurrence_date(master):
    """Date of the last occurrence's end, or None if the series is infinite."""
    rrule_prop = master.get("RRULE")
    if rrule_prop is None:
        return event_end_date(master)  # one-off

    rule_str = rrule_prop.to_ical().decode("utf-8")
    upper = rule_str.upper()
    if "UNTIL" not in upper and "COUNT" not in upper:
        return None  # infinite -> always keep

    dtstart = master.get("DTSTART")
    if dtstart is None:
        return None
    start_dt = as_datetime(dtstart.dt)

    try:
        occurrences = list(rrulestr(rule_str, dtstart=start_dt))
    except Exception:
        # If expansion fails, keep the event rather, than potentially dropping a real one.
        return None

    if not occurrences:
        return event_end_date(master)

    last_start = occurrences[-1]
    start = dtstart.dt
    dtend = master.get("DTEND")
    if isinstance(start, dt.datetime) and dtend is not None and isinstance(dtend.dt, dt.datetime):
        last_start = last_start + (dtend.dt - start)
    return to_date(last_start)


def stabilize(comp):
    """Make a VEVENT byte-stable across re-fetches of an unchanged event.

    Google stamps every export with a fresh DTSTAMP (the time of the fetch),
    which makes vdirsyncer treat every item as changed on each run. Pin DTSTAMP
    to LAST-MODIFIED/CREATED instead, so it only moves when the event really
    changes and the status cache can skip untouched items.
    """
    anchor = comp.get("LAST-MODIFIED") or comp.get("CREATED")
    if anchor is None or comp.get("DTSTAMP") is None:
        return
    comp.pop("DTSTAMP", None)
    comp.add("DTSTAMP", anchor.dt)


def main():
    req = Request(FEED_URL, headers={"User-Agent": "ics-filter/1.0"})
    with urlopen(req, timeout=60) as resp:
        raw = resp.read()

    src = Calendar.from_ical(raw)

    out = Calendar()
    for key in src.keys():
        out.add(key, src[key])

    groups = {}
    passthrough = []
    for comp in src.subcomponents:
        if comp.name == "VEVENT":
            uid = str(comp.get("UID"))
            groups.setdefault(uid, []).append(comp)
        else:
            passthrough.append(comp)

    for comp in passthrough:
        out.add_component(comp)

    kept = 0
    for uid in sorted(groups):  # deterministic output order
        comps = groups[uid]
        master = next((c for c in comps if c.get("RECURRENCE-ID") is None), comps[0])
        last = latest_occurrence_date(master)
        override_dates = [
            event_end_date(c) for c in comps if c.get("RECURRENCE-ID") is not None
        ]
        override_dates = [d for d in override_dates if d is not None]

        # Lower bound: keep if any occurrence ends on/after the start cutoff.
        if last is None:  # infinite series -> always clears the lower bound
            after_start = True
        else:
            after_start = any(d >= CUTOFF_DATE_ENV for d in [last] + override_dates)

        # Upper bound: keep if the event begins on/before the end cutoff.
        starts = [event_start_date(c) for c in comps]
        starts = [d for d in starts if d is not None]
        first = min(starts) if starts else None
        before_end = CUTOFF_END_DATE_ENV is None or first is None or first <= CUTOFF_END_DATE_ENV

        keep = after_start and before_end

        if keep:
            for c in comps:
                stabilize(c)
                out.add_component(c)
            kept += 1

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    tmp = OUTPUT_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out.to_ical())
    os.replace(tmp, OUTPUT_PATH)

    window = f"{CUTOFF_DATE_ENV}..{CUTOFF_END_DATE_ENV or '∞'}"
    print(
        f"window {window}: kept {kept} of {len(groups)} events -> {OUTPUT_PATH}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

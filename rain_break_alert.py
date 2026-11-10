#!/usr/bin/env python3
"""Text me when a rain break starts in ZIP 98664 (Vancouver, WA), 8 AM - 6 PM Pacific.

Data: Open-Meteo 15-minute precipitation (free, no API key).
Alerts: SMS only (via Twilio) to every number in SMS_TO; one text at a 30+ minute dry
window, one upgrade text at a 2+ hour dry window.
Usage:  python rain_break_alert.py            # normal run
        python rain_break_alert.py --dry-run  # print instead of texting, ignore time-of-day
        python rain_break_alert.py --test-sms # send a test text
"""
import json
import os
import base64
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---- Settings you may want to tweak -------------------------------------
TZ = ZoneInfo("America/Los_Angeles")
LAT, LON = 45.6195, -122.5772      # center of ZIP 98664 (east Vancouver, WA)
DAY_START, DAY_END = 8, 18         # 8 AM to 6 PM local
LAST_DAY = date(2027, 4, 30)       # last day alerts are sent; silent after this
MIN_WINDOW_MIN = 30                # first alert tier
LONG_WINDOW_MIN = 120              # second alert tier
LEAD_SLOTS = 2                     # alert up to 2 x 15 min before the break starts
WET_MM = 0.0                       # a 15-min slot is "wet" if precip > this (mm)
STATE_FILE = Path(__file__).with_name("state.json")
# --------------------------------------------------------------------------

SLOT = timedelta(minutes=15)


def fetch_slots():
    qs = urllib.parse.urlencode({
        "latitude": LAT,
        "longitude": LON,
        "minutely_15": "precipitation",
        "past_minutely_15": 8,
        "forecast_minutely_15": 32,
        "timezone": "America/Los_Angeles",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{qs}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)["minutely_15"]
    return [
        (datetime.fromisoformat(t).replace(tzinfo=TZ), p if p is not None else 0.0)
        for t, p in zip(data["time"], data["precipitation"])
    ]


def find_break(slots, now):
    """Return (start, end, minutes) for a rain break starting now or very soon, else None.

    A 'break' = a run of dry slots immediately preceded by a wet slot, so a
    dry sunny day does not text you all day.
    """
    cur = max(i for i, (t, _) in enumerate(slots) if t <= now)
    day_end = now.replace(hour=DAY_END, minute=0, second=0, microsecond=0)
    wet = [p > WET_MM for _, p in slots]

    for d in range(max(cur, 1), min(cur + LEAD_SLOTS, len(slots) - 1) + 1):
        if wet[d] or not wet[d - 1]:
            continue
        start = slots[d][0]
        if start.hour < DAY_START or start >= day_end:
            continue
        i = d
        while i < len(slots) and not wet[i] and slots[i][0] < day_end:
            i += 1
        end = slots[i][0] if i < len(slots) else slots[-1][0] + SLOT
        minutes = int((end - start).total_seconds() // 60)
        if minutes >= MIN_WINDOW_MIN:
            return start, end, minutes
    return None


def fmt_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_len(minutes):
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": {}}


def save_state(state, now):
    cutoff = (now - timedelta(days=2)).isoformat()
    state["sent"] = {k: v for k, v in state["sent"].items() if k >= cutoff[:16]}
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def send_sms(body, dry_run=False):
    """Text every number in SMS_TO (comma-separated, E.164 like +15035551234).

    Returns how many numbers were texted successfully.
    """
    if dry_run:
        print(f"[dry-run] SMS: {body}")
        return 1
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    sender = os.environ["TWILIO_FROM"]
    numbers = [n.strip() for n in os.environ["SMS_TO"].split(",") if n.strip()]
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    ok = 0
    for to in numbers:
        data = urllib.parse.urlencode({"To": to, "From": sender, "Body": body}).encode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
            ok += 1
        except urllib.error.URLError as e:  # keep going so one bad number doesn't block the other
            print(f"SMS to {to[-4:]} failed: {e}", file=sys.stderr)
    return ok


def main():
    dry_run = "--dry-run" in sys.argv
    if "--test-sms" in sys.argv:
        send_sms("Rain-break alert test: texts are working.", dry_run)
        return

    now = datetime.now(TZ)
    if not dry_run and now.date() > LAST_DAY:
        print(f"Past {LAST_DAY}; alerts are finished.")
        return
    if not dry_run and not (DAY_START <= now.hour < DAY_END):
        print("Outside 8 AM-6 PM window; nothing to do.")
        return

    slots = fetch_slots()
    result = find_break(slots, now)
    state = load_state()

    if not result:
        print(f"{now:%H:%M} no qualifying rain break.")
        save_state(state, now)
        return

    start, end, minutes = result
    key = start.isoformat()[:16]
    sent = state["sent"].setdefault(key, [])

    if minutes >= LONG_WINDOW_MIN and "long" not in sent:
        body = (f"Rain break: rain stops around {fmt_time(start)}. "
                f"Dry for 2+ hours, until ~{fmt_time(end)} ({fmt_len(minutes)}).")
        if send_sms(body, dry_run):
            sent += ["short", "long"]
    elif minutes >= MIN_WINDOW_MIN and "short" not in sent and "long" not in sent:
        body = (f"Rain break: dry from {fmt_time(start)} "
                f"until ~{fmt_time(end)} ({fmt_len(minutes)}).")
        if send_sms(body, dry_run):
            sent.append("short")
    else:
        print(f"Break at {key} already texted: {sent}")

    if not dry_run:
        save_state(state, now)


if __name__ == "__main__":
    main()

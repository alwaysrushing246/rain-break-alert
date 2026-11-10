#!/usr/bin/env python3
"""Text me when a rain break starts in ZIP 98664 (Vancouver, WA), 8 AM - 6 PM Pacific.

Data: Open-Meteo 15-minute precipitation (free, no API key). If TOMORROW_API_KEY is set, Tomorrow.io's
minute-by-minute nowcast (free key) replaces Open-Meteo for the next hour, which is usually closer to
what radar-based phone weather apps show.
Alerts, any combination of:
  - NTFY_TOPIC: free push notification to every phone subscribed to that topic in the ntfy app.
  - SMS_TO: comma-separated list. An entry with "@" is a carrier email-to-text address (sent through
    Gmail); an entry without "@" is a phone number like +15035551234 (sent through Twilio).
One alert at a 30+ minute dry window, one upgrade alert at 2+ hours.
Usage:  python rain_break_alert.py            # normal run
        python rain_break_alert.py --dry-run  # print instead of texting, ignore time-of-day
        python rain_break_alert.py --test-sms # send a test text
"""
import base64
import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

# ---- Settings you may want to tweak -------------------------------------
TZ = ZoneInfo("America/Los_Angeles")
LAT, LON = 45.6195, -122.5772      # center of ZIP 98664 (east Vancouver, WA)
ZIP_LOCATION = "98664 US"          # Tomorrow.io looks up this ZIP itself (falls back to LAT/LON)
DAY_START, DAY_END = 8, 18         # 8 AM to 6 PM local
LAST_DAY = date(2027, 4, 30)       # last day alerts are sent; silent after this
MIN_WINDOW_MIN = 30                # first alert tier
LONG_WINDOW_MIN = 120              # second alert tier
LEAD_SLOTS = 2                     # alert up to 2 x 15 min before the break starts
GRACE_SLOTS = 2                    # still alert up to 2 x 15 min after it started (covers delayed runs)
WET_MM = 0.0                       # a 15-min slot is "wet" if precip > this (mm)
# Tomorrow.io rain fields to read (mm/hr); the first present value(s) are used. If none exist but a
# precipitationProbability does, a minute counts as wet at or above PROB_WET_PCT percent.
INTENSITY_FIELDS = ("precipitationIntensity", "rainIntensity", "freezingRainIntensity",
                    "sleetIntensity", "snowIntensity")
PROB_WET_PCT = 50
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


def fetch_tomorrow(key, location=ZIP_LOCATION):
    """Tomorrow.io minute-by-minute nowcast (next ~60 min), bucketed into 15-min slots (mm)."""
    qs = urllib.parse.urlencode({
        "location": location,
        "timesteps": "1m",
        "units": "metric",
        "apikey": key,
    })
    req = urllib.request.Request(
        f"https://api.tomorrow.io/v4/weather/forecast?{qs}",
        headers={"accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    items = data["timelines"].get("minutely", [])
    keys = sorted(items[0]["values"].keys()) if items else []
    print(f"Tomorrow.io minutely fields: {', '.join(keys) or '(none)'}")

    def has_intensity(v):
        return any(v.get(f) is not None for f in INTENSITY_FIELDS)

    use_prob = (
        bool(items)
        and not any(has_intensity(i["values"]) for i in items)
        and any(i["values"].get("precipitationProbability") is not None for i in items)
    )
    if use_prob:
        print(f"No rain-intensity field; using precipitationProbability >= {PROB_WET_PCT}% as rain.")

    buckets = {}
    usable = 0
    peak = 0.0
    for item in items:
        v = item["values"]
        if use_prob:
            pp = v.get("precipitationProbability")
            if pp is None:
                continue
            raw = 1.0 if float(pp) >= PROB_WET_PCT else 0.0  # treated as mm/hr
        else:
            vals = [float(v[f]) for f in INTENSITY_FIELDS if v.get(f) is not None]
            if not vals:  # unknown, NOT dry: skip so Open-Meteo is kept for that slot
                continue
            raw = max(vals)
        usable += 1
        peak = max(peak, raw)
        t = datetime.fromisoformat(item["time"].replace("Z", "+00:00")).astimezone(TZ)
        slot = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
        buckets[slot] = buckets.get(slot, 0.0) + raw / 60.0  # mm/hr -> mm/min
    print(f"Tomorrow.io: {len(items)} minutely points, {usable} usable, peak {peak:.2f} mm/h")
    return sorted(buckets.items())


def merge_nowcast(slots, nowcast):
    """Use the nowcast for any slot it covers; keep Open-Meteo for the rest."""
    override = dict(nowcast)
    return [(t, override.get(t, p)) for t, p in slots]


def find_break(slots, now):
    """Return (start, end, minutes_left) for a rain break starting soon or just started, else None.

    A 'break' = a run of dry slots immediately preceded by a wet slot, so a
    dry sunny day does not text you all day.
    """
    cur = max(i for i, (t, _) in enumerate(slots) if t <= now)
    day_end = now.replace(hour=DAY_END, minute=0, second=0, microsecond=0)
    wet = [p > WET_MM for _, p in slots]

    for d in range(max(cur - GRACE_SLOTS, 1), min(cur + LEAD_SLOTS, len(slots) - 1) + 1):
        if wet[d] or not wet[d - 1]:
            continue
        start = slots[d][0]
        if start.hour < DAY_START or start >= day_end:
            continue
        i = d
        while i < len(slots) and not wet[i] and slots[i][0] < day_end:
            i += 1
        end = slots[i][0] if i < len(slots) else slots[-1][0] + SLOT
        remaining_from = max(start, slots[cur][0])  # don't count time that already passed
        minutes = int((end - remaining_from).total_seconds() // 60)
        if minutes >= MIN_WINDOW_MIN:
            return start, end, minutes
    return None


def forecast_line(label, slots, now):
    """One log line of 15-min slots near now: * = rain, - = dry."""
    if not slots:
        return f"{label}: (no data)"
    idx = [i for i, (t, _) in enumerate(slots) if t <= now]
    cur = idx[-1] if idx else 0
    part = slots[max(cur - 2, 0):cur + 13]
    cells = " ".join(f"{t:%H:%M}{'*' if p > WET_MM else '-'}" for t, p in part)
    return f"{label} (* = rain, - = dry): {cells}"


def fmt_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_len(minutes):
    h, m = divmod(minutes, 60)
    parts = []
    if h:
        parts.append(f"{h} hr")
    if m:
        parts.append(f"{m} min")
    return " ".join(parts)


def build_message(start, end, minutes, now, update=False):
    """Text that leads with how long the break will last."""
    length = fmt_len(minutes)
    if start <= now + timedelta(minutes=7):
        core = f"no rain for the next {length} (until ~{fmt_time(end)})."
    else:
        core = f"rain stops ~{fmt_time(start)}, then dry for {length} (until ~{fmt_time(end)})."
    return ("Rain break update: " if update else "Rain break: ") + core


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": {}}


def save_state(state, now):
    cutoff = (now - timedelta(days=2)).isoformat()
    state["sent"] = {k: v for k, v in state["sent"].items() if k >= cutoff[:16]}
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _smtp_connect(user, password):
    """Log in to Gmail, trying SSL on 465 first, then STARTTLS on 587."""
    last = None
    for port, use_ssl in ((465, True), (587, False)):
        try:
            if use_ssl:
                smtp = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=30)
            else:
                smtp = smtplib.SMTP("smtp.gmail.com", port, timeout=30)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            smtp.login(user, password)
            return smtp
        except smtplib.SMTPAuthenticationError as e:
            print(f"Gmail rejected the login (port {port}): {e}", file=sys.stderr)
            raise  # wrong user or app password; another port won't help
        except (smtplib.SMTPException, OSError) as e:
            print(f"SMTP port {port} failed: {type(e).__name__}: {e}", file=sys.stderr)
            last = e
    raise last


def _send_gateway(body, addresses):
    """Carrier email-to-text via Gmail SMTP. Returns number of successes."""
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not user or not password:
        print(
            "Missing Gmail secret(s): "
            f"GMAIL_USER={'ok' if user else 'EMPTY'}, "
            f"GMAIL_APP_PASSWORD={'ok' if password else 'EMPTY'}",
            file=sys.stderr,
        )
        return 0
    ok = 0
    try:
        smtp = _smtp_connect(user, password)
    except (smtplib.SMTPException, OSError):
        return 0
    with smtp:
        for to in addresses:
            msg = EmailMessage()  # no subject: the body is the whole text
            msg["From"] = user
            msg["To"] = to
            msg.set_content(body)
            try:
                smtp.send_message(msg)
                ok += 1
            except smtplib.SMTPException as e:  # one bad address must not block the other
                print(f"SMS to {to.split('@')[0][-4:]} failed: {e}", file=sys.stderr)
    return ok


def _send_twilio(body, numbers):
    """Real SMS through Twilio's API. Returns number of successes."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.environ.get("TWILIO_FROM", "").strip()
    if not (sid and token and sender):
        print(
            "Missing Twilio secret(s): "
            f"TWILIO_ACCOUNT_SID={'ok' if sid else 'EMPTY'}, "
            f"TWILIO_AUTH_TOKEN={'ok' if token else 'EMPTY'}, "
            f"TWILIO_FROM={'ok' if sender else 'EMPTY'}",
            file=sys.stderr,
        )
        return 0
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    ok = 0
    for to in numbers:
        data = urllib.parse.urlencode({"To": to, "From": sender, "Body": body}).encode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
            ok += 1
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            print(f"SMS to ...{to[-4:]} failed: HTTP {e.code} {detail}", file=sys.stderr)
        except (urllib.error.URLError, OSError) as e:
            print(f"SMS to ...{to[-4:]} failed: {e}", file=sys.stderr)
    return ok


def _send_ntfy(body):
    """Free push notification through ntfy.sh to every phone subscribed to the topic."""
    topic = os.environ.get("NTFY_TOPIC", "").strip().strip("/")
    if not topic:
        return 0
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=body.encode("utf-8"),
        headers={"Title": "Rain break", "Priority": "high", "Tags": "umbrella"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return 1
    except urllib.error.HTTPError as e:
        print(f"ntfy failed: HTTP {e.code} {e.read().decode(errors='replace')[:200]}", file=sys.stderr)
    except (urllib.error.URLError, OSError) as e:
        print(f"ntfy failed: {e}", file=sys.stderr)
    return 0


def send_sms(body, dry_run=False):
    """Alert every configured channel. Returns the number of successful deliveries."""
    if dry_run:
        print(f"[dry-run] ALERT: {body}")
        return 1
    targets = [t.strip() for t in os.environ.get("SMS_TO", "").split(",") if t.strip()]
    has_ntfy = bool(os.environ.get("NTFY_TOPIC", "").strip())
    if not targets and not has_ntfy:
        print("Nothing configured: set NTFY_TOPIC and/or SMS_TO.", file=sys.stderr)
        return 0
    gateways = [t for t in targets if "@" in t]
    phones = [t for t in targets if "@" not in t]
    ok = 0
    if has_ntfy:
        n = _send_ntfy(body)
        print(f"ntfy push: {'sent' if n else 'FAILED'}")
        ok += n
    if phones:
        ok += _send_twilio(body, phones)
    if gateways:
        ok += _send_gateway(body, gateways)
    print(f"Delivered {ok} alert(s) across {len(targets) + int(has_ntfy)} channel(s)/recipient(s).")
    return ok


def main():
    dry_run = "--dry-run" in sys.argv
    if "--test-sms" in sys.argv:
        if not send_sms("Rain-break alert test: texts are working.", dry_run):
            sys.exit("Test text failed: no message was sent. Check the secrets.")
        return

    now = datetime.now(TZ)
    if not dry_run and now.date() > LAST_DAY:
        print(f"Past {LAST_DAY}; alerts are finished.")
        return
    if not dry_run and not (DAY_START <= now.hour < DAY_END):
        print("Outside 8 AM-6 PM window; nothing to do.")
        return

    slots = fetch_slots()
    print(forecast_line("Open-Meteo", slots, now))
    tomorrow_key = os.environ.get("TOMORROW_API_KEY", "").strip()
    if tomorrow_key:
        try:
            try:
                nowcast = fetch_tomorrow(tomorrow_key, ZIP_LOCATION)
                print(f"Tomorrow.io location: {ZIP_LOCATION}")
            except urllib.error.HTTPError as e:
                if e.code not in (400, 404):
                    raise
                print(f"Tomorrow.io rejected '{ZIP_LOCATION}' (HTTP {e.code}); using coordinates.")
                nowcast = fetch_tomorrow(tomorrow_key, f"{LAT},{LON}")
            if nowcast:
                print(forecast_line("Tomorrow.io nowcast", nowcast, now))
                slots = merge_nowcast(slots, nowcast)
                print(forecast_line("Used for alerts", slots, now))
            else:
                print("Tomorrow.io returned no usable rain readings; using Open-Meteo only.")
        except urllib.error.HTTPError as e:
            print(f"Tomorrow.io failed (HTTP {e.code}); using Open-Meteo only.", file=sys.stderr)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            print(f"Tomorrow.io failed ({type(e).__name__}: {e}); using Open-Meteo only.", file=sys.stderr)
    result = find_break(slots, now)
    state = load_state()

    if not result:
        print(f"{now:%H:%M} no rain break of {MIN_WINDOW_MIN}+ min starting within "
              f"the next {LEAD_SLOTS * 15} min (or begun in the last {GRACE_SLOTS * 15}), so no alert.")
        save_state(state, now)
        return

    start, end, minutes = result
    key = start.isoformat()[:16]
    sent = state["sent"].setdefault(key, [])

    if minutes >= LONG_WINDOW_MIN and "long" not in sent:
        body = build_message(start, end, minutes, now, update=bool(sent))
        if send_sms(body, dry_run):
            sent += ["short", "long"]
    elif minutes >= MIN_WINDOW_MIN and "short" not in sent and "long" not in sent:
        body = build_message(start, end, minutes, now)
        if send_sms(body, dry_run):
            sent.append("short")
    else:
        print(f"Break at {key} already texted: {sent}")

    if not dry_run:
        save_state(state, now)


if __name__ == "__main__":
    main()

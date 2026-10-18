#!/usr/bin/env python3
"""apply-log: a private, local job-hunt tracker.

It keeps your applications in one JSON file on your own machine: which company and role, which
resume variant you sent, what happened and when, and when to follow up. It can list what is
due, report which resume variants got replies, and export reminders as a CSV that Todoist can
import. It never sends anything anywhere.
"""
import argparse
import csv
import io
import json
import os
import statistics
import sys
import tempfile
from datetime import date, datetime, timedelta

__version__ = "0.2.0"

STATUSES = ["wishlist", "applied", "screening", "interview", "offer", "accepted", "rejected", "withdrawn", "ghosted"]
OPEN = ("wishlist", "applied", "screening", "interview", "offer")
CLOSED = ("accepted", "rejected", "withdrawn", "ghosted")
REPLIED = ("screening", "interview", "offer", "accepted", "rejected")   # a company answered, even if it was a no
ADVANCED = ("screening", "interview", "offer", "accepted")              # a company moved you forward
DEFAULT_FOLLOW_UP_DAYS = 7
SMALL_SAMPLE = 5
LIMITS = {"company": 100, "role": 100, "url": 500, "resume": 40, "source": 60, "contact": 200, "note": 1000}
DEFAULT_FILE = os.path.join(os.path.expanduser("~"), ".apply-log", "applications.json")
TODOIST_HEADER = ["TYPE", "CONTENT", "DESCRIPTION", "PRIORITY", "INDENT", "AUTHOR", "RESPONSIBLE", "DATE", "DATE_LANG", "TIMEZONE"]


class ApplyLogError(Exception):
    """Something the user can fix: a bad value, an unknown id, a damaged file."""


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_date(text, today):
    """YYYY-MM-DD, today, tomorrow, or +Nd / +Nw for days or weeks from today."""
    raw = str(text).strip().lower()
    if raw == "today":
        return today
    if raw == "tomorrow":
        return today + timedelta(days=1)
    if len(raw) >= 3 and raw[0] == "+" and raw[-1] in "dw" and raw[1:-1].isdigit():
        return today + timedelta(days=int(raw[1:-1]) * (7 if raw[-1] == "w" else 1))
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ApplyLogError(f"cannot read the date '{text}': use YYYY-MM-DD, today, tomorrow, +7d or +2w")


def iso(value):
    return value.isoformat() if value else None


def from_iso(text):
    return date.fromisoformat(text) if text else None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def empty_data():
    return {"version": 1, "next_id": 1, "applications": []}


def validate_application(app):
    where = f"application {app.get('id', '?')}"
    for key in ("id", "company", "role", "status", "history"):
        if key not in app:
            raise ApplyLogError(f"{where} is missing '{key}'")
    if not isinstance(app["id"], int) or app["status"] not in STATUSES or not isinstance(app["history"], list):
        raise ApplyLogError(f"{where} has an invalid id, status or history")
    for key in ("applied_on", "follow_up", "created", "updated"):
        try:
            from_iso(app.get(key))
        except (TypeError, ValueError):
            raise ApplyLogError(f"{where} has an invalid date in '{key}'")


def load(path):
    if not os.path.exists(path):
        return empty_data()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as err:
        raise ApplyLogError(f"cannot read {path}: {err}")
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("applications"), list):
        raise ApplyLogError(f"{path} is not an apply-log file (version 1)")
    ids = set()
    for app in data["applications"]:
        if not isinstance(app, dict):
            raise ApplyLogError(f"{path} contains an entry that is not an object")
        validate_application(app)
        if app["id"] in ids:
            raise ApplyLogError(f"{path} has two applications with id {app['id']}")
        ids.add(app["id"])
    data["next_id"] = max(int(data.get("next_id", 1)), max(ids, default=0) + 1)
    return data


def save(path, data):
    """Write the whole file atomically and keep it private to your user."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".apply-log-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def find(data, app_id):
    for app in data["applications"]:
        if app["id"] == app_id:
            return app
    raise ApplyLogError(f"there is no application with id {app_id}")


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------

def clean(value, field, required=False):
    text = " ".join(str(value).split()) if value is not None else ""
    if required and not text:
        raise ApplyLogError(f"{field} cannot be empty")
    if len(text) > LIMITS[field]:
        raise ApplyLogError(f"{field} is {len(text)} characters; the limit is {LIMITS[field]}")
    if field == "url" and text and not text.lower().startswith(("http://", "https://")):
        raise ApplyLogError("url must start with http:// or https://")
    return text or None


def log(app, today, event, **fields):
    app["history"].append({"on": iso(today), "event": event, **{k: v for k, v in fields.items() if v is not None}})
    app["updated"] = iso(today)


def add_application(data, today, company, role, status="applied", url=None, resume=None, source=None, contact=None,
                    note=None, applied_on=None, follow_up=None, follow_up_in=None):
    if status not in STATUSES:
        raise ApplyLogError(f"status must be one of: {', '.join(STATUSES)}")
    applied = applied_on if applied_on is not None else (today if status != "wishlist" else None)
    if follow_up is None and status in ("applied", "screening", "interview"):
        follow_up = applied + timedelta(days=follow_up_in if follow_up_in is not None else DEFAULT_FOLLOW_UP_DAYS) if applied else None
    app = {
        "id": data["next_id"], "company": clean(company, "company", True), "role": clean(role, "role", True),
        "url": clean(url, "url"), "resume": clean(resume, "resume"), "source": clean(source, "source"),
        "contact": clean(contact, "contact"), "status": status, "applied_on": iso(applied),
        "follow_up": iso(follow_up), "notes": clean(note, "note"), "created": iso(today), "updated": iso(today), "history": [],
    }
    log(app, today, "created", to=status, note=app["notes"])
    data["next_id"] += 1
    data["applications"].append(app)
    return app


def update_application(data, today, app_id, status=None, note=None, follow_up=..., **fields):
    """Change fields. `follow_up` may be a date, None (clear it) or left out (unchanged)."""
    app = find(data, app_id)
    final_status = status if status is not None else app["status"]
    if follow_up is not ... and follow_up is not None and final_status in CLOSED:
        raise ApplyLogError(f"a {final_status} application has no follow-up date")      # checked first so nothing is half-updated
    for field in ("company", "role", "url", "resume", "source", "contact"):
        if field in fields and fields[field] is not None:
            value = clean(fields[field], field, required=field in ("company", "role"))
            if value != app.get(field):
                log(app, today, "edited", field=field, to=value)
            app[field] = value
    if fields.get("applied_on") is not None:
        value = iso(fields["applied_on"])
        if value != app.get("applied_on"):
            log(app, today, "edited", field="applied_on", to=value)
        app["applied_on"] = value
    if status is not None and status != app["status"]:
        if status not in STATUSES:
            raise ApplyLogError(f"status must be one of: {', '.join(STATUSES)}")
        old = app["status"]
        app["status"] = status
        log(app, today, "status", **{"from": old, "to": status, "note": clean(note, "note")})
        note = None
        if status != "wishlist" and not app["applied_on"]:
            app["applied_on"] = iso(today)
        if status in CLOSED:
            app["follow_up"] = None
        elif follow_up is ... and not app["follow_up"] and status in ("applied", "screening", "interview"):
            app["follow_up"] = iso(today + timedelta(days=DEFAULT_FOLLOW_UP_DAYS))
    if follow_up is not ...:
        app["follow_up"] = iso(follow_up)
        log(app, today, "follow-up date", to=app["follow_up"])
    if note:
        log(app, today, "note", note=clean(note, "note"))
    app["updated"] = iso(today)
    return app


def record_follow_up(data, today, app_id, next_in=DEFAULT_FOLLOW_UP_DAYS, note=None):
    app = find(data, app_id)
    if app["status"] in CLOSED:
        raise ApplyLogError(f"application {app_id} is {app['status']}; there is nothing to follow up")
    log(app, today, "followed up", note=clean(note, "note"))
    app["follow_up"] = iso(today + timedelta(days=next_in))
    return app, sum(1 for h in app["history"] if h["event"] == "followed up")


def remove_application(data, app_id):
    app = find(data, app_id)
    data["applications"].remove(app)
    return app


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def due(data, today, within=0):
    """Open applications whose follow-up date is on or before today + within days, soonest first."""
    limit = today + timedelta(days=within)
    items = [a for a in data["applications"] if a["status"] in OPEN and a.get("follow_up") and from_iso(a["follow_up"]) <= limit]
    return sorted(items, key=lambda a: (a["follow_up"], a["id"]))


def stale(data, today, days=30):
    """Open applications nobody has touched for `days` days (not counting the wishlist)."""
    cutoff = today - timedelta(days=days)
    return [a for a in data["applications"] if a["status"] in OPEN and a["status"] != "wishlist" and from_iso(a["updated"]) <= cutoff]


def first_reply_days(app):
    applied = from_iso(app.get("applied_on"))
    if not applied:
        return None
    for event in app["history"]:
        if event["event"] == "status" and event.get("to") in REPLIED and event.get("from") in ("applied", "wishlist", "ghosted"):
            return max(0, (from_iso(event["on"]) - applied).days)
    return None


def stats(data, today):
    apps = data["applications"]
    sent = [a for a in apps if a["status"] != "wishlist" and a.get("applied_on")]

    def reached(app, group):
        return app["status"] in group or any(e["event"] == "status" and e.get("to") in group for e in app["history"])

    def tally(items):
        return {"applied": len(items), "replied": sum(1 for a in items if reached(a, REPLIED)),
                "advanced": sum(1 for a in items if reached(a, ADVANCED))}

    by_resume, by_source = {}, {}
    for app in sent:
        by_resume.setdefault(app.get("resume") or "(none)", []).append(app)
        by_source.setdefault(app.get("source") or "(none)", []).append(app)
    delays = [d for d in (first_reply_days(a) for a in sent) if d is not None]
    return {
        "total": len(apps), "open": sum(1 for a in apps if a["status"] in OPEN),
        "by_status": {s: sum(1 for a in apps if a["status"] == s) for s in STATUSES if any(a["status"] == s for a in apps)},
        "sent": tally(sent), "last_7_days": sum(1 for a in sent if from_iso(a["applied_on"]) > today - timedelta(days=7)),
        "last_30_days": sum(1 for a in sent if from_iso(a["applied_on"]) > today - timedelta(days=30)),
        "median_days_to_first_reply": statistics.median(delays) if delays else None, "replies_with_known_delay": len(delays),
        "by_resume": {k: tally(v) for k, v in sorted(by_resume.items())}, "by_source": {k: tally(v) for k, v in sorted(by_source.items())},
        "small_sample_below": SMALL_SAMPLE,
    }


def _safe_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def todoist_csv(data, today, within=14):
    """A CSV in Todoist's import layout: one task per open application with a follow-up date inside the window."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(TODOIST_HEADER)
    for app in due(data, today, within):
        parts = [f"apply-log #{app['id']}", f"status {app['status']}"]
        if app.get("resume"):
            parts.append(f"resume {app['resume']}")
        if app.get("url"):
            parts.append(app["url"])
        writer.writerow([_safe_cell(v) for v in ("task", f"Follow up: {app['company']}, {app['role']}", " | ".join(parts), 3, 1, "", "", app["follow_up"], "en", "")])
    return out.getvalue()


def export_csv(data):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    columns = ["id", "company", "role", "status", "resume", "source", "applied_on", "follow_up", "url", "contact", "notes", "created", "updated"]
    writer.writerow(columns)
    for app in data["applications"]:
        writer.writerow([_safe_cell(app.get(c)) for c in columns])
    return out.getvalue()


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

def table(rows, headers):
    cut = lambda text, width: text if len(text) <= width else text[:width - 1] + "…"  # noqa: E731
    cells = [[cut("" if v is None else str(v), 30) for v in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip(), "  ".join("-" * w for w in widths)]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]
    return "\n".join(lines)


def list_rows(apps):
    return [[a["id"], a["status"], a["company"], a["role"], a.get("resume") or "", a.get("applied_on") or "", a.get("follow_up") or ""] for a in apps]


LIST_HEADERS = ["ID", "STATUS", "COMPANY", "ROLE", "RESUME", "APPLIED", "FOLLOW-UP"]


def render_show(app):
    lines = [f"#{app['id']}  {app['company']}, {app['role']}", f"status: {app['status']}"]
    for label, key in (("resume", "resume"), ("source", "source"), ("applied", "applied_on"), ("follow up", "follow_up"), ("link", "url"), ("contact", "contact"), ("notes", "notes")):
        if app.get(key):
            lines.append(f"{label}: {app[key]}")
    lines.append("history:")
    for event in app["history"]:
        detail = " ".join(f"{k}={v}" for k, v in event.items() if k not in ("on", "event"))
        lines.append(f"  {event['on']}  {event['event']}  {detail}".rstrip())
    return "\n".join(lines)


def render_stats(s):
    sent = s["sent"]
    pct = lambda n, d: f"{n / d:.0%}" if d else "n/a"  # noqa: E731
    lines = [f"Applications: {s['total']} ({s['open']} open)",
             "By status: " + (", ".join(f"{k} {v}" for k, v in s["by_status"].items()) or "none"),
             f"Sent in the last 7 days: {s['last_7_days']}, in the last 30 days: {s['last_30_days']}",
             f"Sent: {sent['applied']}. Replies (including rejections): {sent['replied']} ({pct(sent['replied'], sent['applied'])}). "
             f"Moved forward (screen or beyond): {sent['advanced']} ({pct(sent['advanced'], sent['applied'])})."]
    if s["median_days_to_first_reply"] is not None:
        lines.append(f"Median days to a first reply: {s['median_days_to_first_reply']:g} (from {s['replies_with_known_delay']} application(s))")
    for title, group in (("resume variant", s["by_resume"]), ("source", s["by_source"])):
        if group:
            lines += ["", f"By {title}:"]
            for name, t in group.items():
                note = "  (too few to compare)" if t["applied"] < s["small_sample_below"] else ""
                lines.append(f"  {name:<20} sent {t['applied']:>3}  replies {t['replied']:>3}  moved forward {t['advanced']:>3}{note}")
    if sent["applied"] < s["small_sample_below"] * 2:
        lines += ["", f"With fewer than {s['small_sample_below']} applications in a group, differences are noise, not evidence."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(prog="apply-log", description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", help=f"the data file (default: $APPLY_LOG_FILE or {DEFAULT_FILE})")
    ap.add_argument("--version", action="version", version=f"apply-log {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="record an application")
    add.add_argument("company")
    add.add_argument("role")
    add.add_argument("--status", default="applied", choices=STATUSES)
    add.add_argument("--url")
    add.add_argument("--resume", help="which resume variant you sent, for example general or ai-engineer")
    add.add_argument("--source", help="where you found it: referral, LinkedIn, company site ...")
    add.add_argument("--contact", help="a person you are in touch with")
    add.add_argument("--note")
    add.add_argument("--date", help="when you applied (default today)")
    add.add_argument("--follow-up", help="follow-up date (default: a week after applying)")

    lst = sub.add_parser("list", help="show applications")
    lst.add_argument("--status", choices=STATUSES)
    lst.add_argument("--company", help="only companies containing this text")
    lst.add_argument("--resume")
    lst.add_argument("--source", help="only applications from this source")
    lst.add_argument("--all", action="store_true", help="include closed applications")
    lst.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one application with its history")
    show.add_argument("id", type=int)

    upd = sub.add_parser("update", help="change an application")
    upd.add_argument("id", type=int)
    upd.add_argument("--status", choices=STATUSES)
    upd.add_argument("--note")
    upd.add_argument("--follow-up", help="a date, or none to clear it")
    for field in ("company", "role", "url", "resume", "source", "contact"):
        upd.add_argument(f"--{field}")
    upd.add_argument("--applied-on")

    follow = sub.add_parser("follow", help="log that you followed up and schedule the next one")
    follow.add_argument("id", type=int)
    follow.add_argument("--next-in", type=int, default=DEFAULT_FOLLOW_UP_DAYS, help="days until the next follow-up (default 7)")
    follow.add_argument("--note")

    remind = sub.add_parser("remind", help="list follow-ups that are due")
    remind.add_argument("--within", type=int, default=0, help="also include follow-ups due in the next N days")
    remind.add_argument("--json", action="store_true", help="output reminders as JSON")

    stat = sub.add_parser("stats", help="replies by resume variant and source")
    stat.add_argument("--json", action="store_true")

    todoist = sub.add_parser("export-todoist", help="write follow-up reminders as a CSV that Todoist can import")
    todoist.add_argument("--within", type=int, default=14, help="include follow-ups due in the next N days (default 14)")
    todoist.add_argument("--out", help="write to this file instead of the screen")

    export = sub.add_parser("export", help="write every application as CSV or JSON")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--open-only", action="store_true", help="only include open applications")
    export.add_argument("--out")

    rm = sub.add_parser("remove", help="delete an application")
    rm.add_argument("id", type=int)
    rm.add_argument("--yes", action="store_true", help="really delete it")

    sub.add_parser("check", help="check the file and point out applications that need attention")
    return ap


def write_out(text, out):
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {out}")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def run(args, data, today):
    """Run one command. Returns (new_data_or_None_if_unchanged, text_to_print)."""
    cmd = args.command
    if cmd == "add":
        applied = parse_date(args.date, today) if args.date else None
        follow = parse_date(args.follow_up, today) if args.follow_up else None
        app = add_application(data, today, args.company, args.role, args.status, args.url, args.resume, args.source, args.contact,
                              args.note, applied, follow)
        return True, f"added #{app['id']}: {app['company']}, {app['role']} ({app['status']})" + (f", follow up on {app['follow_up']}" if app["follow_up"] else "")
    if cmd == "list":
        apps = [a for a in data["applications"] if (args.all or a["status"] in OPEN)]
        if args.status:
            apps = [a for a in data["applications"] if a["status"] == args.status]
        if args.company:
            apps = [a for a in apps if args.company.lower() in a["company"].lower()]
        if args.resume:
            apps = [a for a in apps if (a.get("resume") or "").lower() == args.resume.lower()]
        if args.source:
            apps = [a for a in apps if args.source.lower() in (a.get("source") or "").lower()]
        apps.sort(key=lambda a: a["id"])
        if args.json:
            return False, json.dumps(apps, indent=2, ensure_ascii=False)
        return False, table(list_rows(apps), LIST_HEADERS) + f"\n{len(apps)} application(s)" if apps else "nothing to show"
    if cmd == "show":
        return False, render_show(find(data, args.id))
    if cmd == "update":
        follow = ...
        if args.follow_up is not None:
            follow = None if args.follow_up.lower() == "none" else parse_date(args.follow_up, today)
        fields = {k: getattr(args, k) for k in ("company", "role", "url", "resume", "source", "contact")}
        fields["applied_on"] = parse_date(args.applied_on, today) if args.applied_on else None
        app = update_application(data, today, args.id, status=args.status, note=args.note, follow_up=follow, **fields)
        return True, f"updated #{app['id']}: {app['company']}, {app['role']} ({app['status']})" + (f", follow up on {app['follow_up']}" if app["follow_up"] else "")
    if cmd == "follow":
        app, count = record_follow_up(data, today, args.id, args.next_in, args.note)
        text = f"logged follow-up #{count} for #{app['id']}; next one on {app['follow_up']}"
        if count >= 3:
            text += f"\nThat is {count} follow-ups with no change. It may be time to set the status to ghosted: apply-log update {app['id']} --status ghosted"
        return True, text
    if cmd == "remind":
        items = due(data, today, args.within)
        if args.json:
            out_items = []
            for a in items:
                days = (from_iso(a["follow_up"]) - today).days
                out_items.append({"id": a["id"], "company": a["company"], "role": a["role"],
                                  "follow_up": a["follow_up"], "days_overdue": -days if days < 0 else 0,
                                  "days_until": days if days >= 0 else 0})
            return False, json.dumps(out_items, indent=2, ensure_ascii=False)
        if not items:
            return False, "no follow-ups due" + (f" in the next {args.within} day(s)" if args.within else "")
        rows = []
        for a in items:
            days = (from_iso(a["follow_up"]) - today).days
            when = f"{-days} day(s) overdue" if days < 0 else "today" if days == 0 else f"in {days} day(s)"
            rows.append([a["id"], a["company"], a["role"], a["follow_up"], when])
        return False, table(rows, ["ID", "COMPANY", "ROLE", "FOLLOW-UP", "WHEN"])
    if cmd == "stats":
        s = stats(data, today)
        return False, json.dumps(s, indent=2) if args.json else render_stats(s)
    if cmd == "export-todoist":
        return False, ("__file__", todoist_csv(data, today, args.within), args.out)
    if cmd == "export":
        apps = [a for a in data["applications"] if a["status"] in OPEN] if args.open_only else data["applications"]
        export_data = {"version": data.get("version", 1), "applications": apps}
        text = export_csv(export_data) if args.format == "csv" else json.dumps(apps, indent=2, ensure_ascii=False) + "\n"
        return False, ("__file__", text, args.out)
    if cmd == "remove":
        app = find(data, args.id)
        if not args.yes:
            raise ApplyLogError(f"this would delete #{app['id']} ({app['company']}, {app['role']}) and its history; add --yes to confirm")
        remove_application(data, args.id)
        return True, f"removed #{app['id']}"
    if cmd == "check":
        lines = [f"file is valid: {len(data['applications'])} application(s)"]
        for a in data["applications"]:
            if a["status"] in ("applied", "screening", "interview") and not a.get("follow_up"):
                lines.append(f"  #{a['id']} {a['company']}: {a['status']} but no follow-up date")
        for a in stale(data, today):
            lines.append(f"  #{a['id']} {a['company']}: no update since {a['updated']}")
        return False, "\n".join(lines)
    raise ApplyLogError(f"unknown command {cmd}")


def main(argv=None, today=None):
    args = build_parser().parse_args(argv)
    path = args.file or os.environ.get("APPLY_LOG_FILE") or DEFAULT_FILE
    try:
        if today is None:
            override = os.environ.get("APPLY_LOG_TODAY")   # pretend it is another day: for demos and tests
            try:
                today = date.fromisoformat(override) if override else date.today()
            except ValueError:
                raise ApplyLogError(f"APPLY_LOG_TODAY must be YYYY-MM-DD, not '{override}'")
        data = load(path)
        changed, result = run(args, data, today)
        if changed:
            save(path, data)
        if isinstance(result, tuple):
            write_out(result[1], result[2])
        else:
            print(result)
        return 0
    except ApplyLogError as err:
        print(f"apply-log: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

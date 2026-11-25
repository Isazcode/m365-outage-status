#!/usr/bin/env python3
"""Build platforms.json for the multi-platform status dashboard.

Sources (all free, no logins):
  * ICE Status Center public feed (ice.com) for WebICE markets, ICE Chat, ICE Connect, clearing.
  * Simple "does the website answer" checks for vendors without a public status page
    (Bloomberg, Cloud9, KRM22).
  * manual.json for statuses set by hand (e.g. the desk reports Bloomberg is slow).
  * status.json (Microsoft 365) for the Microsoft tab.

Runs in GitHub Actions; writes platforms.json only when something meaningful changed,
or once an hour as a heartbeat, so the repo isn't flooded with commits.
"""
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
CONFIG = os.path.join(ROOT, "platforms.config.json")
MANUAL = os.path.join(ROOT, "manual.json")
M365 = os.path.join(ROOT, "status.json")
OUT = os.path.join(ROOT, "platforms.json")
ICE_FEED = "https://www.ice.com/api/notifications/public/status-center/ice/summary"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", "15"))
HEARTBEAT_MIN = 60

RANK = {"healthy": 0, "unknown": 1, "maintenance": 2, "degraded": 3, "outage": 4}
LABEL = {"healthy": "Operational", "degraded": "Degraded", "outage": "Outage",
         "maintenance": "Maintenance", "unknown": "Unknown"}
ICE_SEV = {
    "normal": "healthy",
    "informational message": "healthy",
    "systems affected": "degraded",
    "critical issue": "outage",
}


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def worst(statuses, default="unknown"):
    statuses = [s for s in statuses if s]
    return max(statuses, key=lambda s: RANK.get(s, 1)) if statuses else default


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def check_url(url):
    """Return (ok, http_code_or_None, ms). Any HTTP answer below 500 counts as 'responding'."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        return False, None, int((time.monotonic() - t0) * 1000)
    return code < 500, code, int((time.monotonic() - t0) * 1000)


def active_manual(manual, pid):
    m = (manual or {}).get(pid) or {}
    if not m.get("status"):
        return None
    until = m.get("until")
    if until:
        try:
            if datetime.fromisoformat(until.replace("Z", "+00:00")) < now():
                return None
        except ValueError:
            pass
    return m


# ---------------------------------------------------------------- ICE
def build_ice(p):
    try:
        feed = fetch_json(ICE_FEED)
    except Exception as e:
        return {"overall": "unknown", "note": f"Couldn't read the ICE Status Center feed ({type(e).__name__}).",
                "source": "ICE Status Center (unreachable)", "components": [], "maintenance": []}

    plats = {}
    alerts = []
    for unit in feed.get("majorBusinessUnits", []):
        for seg in unit.get("segments", []):
            for pl in seg.get("platforms", []):
                sev = (((pl.get("systemStatus") or {}).get("severity") or {}).get("label") or "Normal")
                plats[pl["uniqueId"]] = {"name": pl["name"], "label": sev,
                                         "status": ICE_SEV.get(sev.lower(), "unknown")}
        for a in unit.get("maintenanceAlerts", []) or []:
            alerts.append(a)

    t_now = now()
    comps, tracked_names = [], set()
    for c in p.get("components", []):
        items = []
        for pid in c.get("iceIds", []):
            pl = plats.get(pid)
            if pl:
                items.append(pl)
                tracked_names.add(pl["name"])
            else:
                items.append({"name": pid, "label": "Not listed", "status": "unknown"})
        comps.append({"name": c["name"], "note": c.get("note", ""),
                      "status": worst([i["status"] for i in items]), "items": items})

    maint = []
    for a in alerts:
        names = set(a.get("affectedPlatforms") or [])
        if not names & tracked_names:
            continue
        try:
            start = datetime.fromtimestamp(a["startDate"] / 1000, timezone.utc)
            end = datetime.fromtimestamp(a["endDate"] / 1000, timezone.utc) if a.get("endDate") else None
        except Exception:
            continue
        if end and end < t_now:
            continue
        if start > t_now + timedelta(days=7):
            continue
        active = start <= t_now and (end is None or end >= t_now)
        maint.append({"subject": a.get("subject") or "Maintenance", "start": iso(start),
                      "end": iso(end) if end else None, "active": active,
                      "message": clean(a.get("message"))[:400], "platforms": sorted(names & tracked_names)})
        if active:
            for c in comps:
                if any(i["name"] in names for i in c["items"]) and c["status"] == "healthy":
                    c["status"] = "maintenance"
    maint.sort(key=lambda m: m["start"])

    overall = worst([c["status"] for c in comps], "healthy")
    bad = [c["name"] for c in comps if c["status"] in ("degraded", "outage")]
    if bad:
        note = "ICE reports issues affecting: " + ", ".join(bad) + "."
    elif any(c["status"] == "maintenance" for c in comps):
        note = "Scheduled maintenance in progress on some ICE systems."
    else:
        note = "All tracked ICE systems normal per the ICE Status Center."
    return {"overall": overall, "note": note, "source": "ICE Status Center (automatic)",
            "components": comps, "maintenance": maint}


# ---------------------------------------------------------------- website checks
def build_checks(p):
    checks = []
    for c in p.get("checks", []):
        ok, code, ms = check_url(c["url"])
        checks.append({"name": c["name"], "url": c["url"], "ok": ok, "code": code, "ms": ms})
    if not checks:
        return {"overall": "unknown", "note": "No checks configured.", "source": "None", "checks": []}
    down = [c["name"] for c in checks if not c["ok"]]
    if down:
        overall = "degraded" if len(down) < len(checks) else "outage"
        note = "Not responding: " + ", ".join(down) + ". This may or may not affect the app itself."
    else:
        overall = "healthy"
        note = "Websites responding. No official status feed, so app-level problems may not show here."
    return {"overall": overall, "note": note, "source": "Website checks (automatic)", "checks": checks}


# ---------------------------------------------------------------- Microsoft 365
def build_m365():
    try:
        with open(M365) as f:
            s = json.load(f)
    except Exception:
        return {"overall": "unknown", "note": "No Microsoft 365 data.", "source": "status.json"}
    if s.get("resolved"):
        return {"overall": "healthy",
                "note": "No active incident being tracked. Last: Nov 12–14 global outage (EX1550543 / MO1550821), resolved.",
                "source": "Microsoft 365 tab", "lastIncident": s.get("resolvedAt")}
    return {"overall": s.get("overall", "unknown"), "note": s.get("overallNote", ""),
            "source": "Microsoft 365 tab", "lastIncident": None}


def m365_extra():
    try:
        with open(M365) as f:
            s = json.load(f)
    except Exception:
        return {}
    return {"advisories": s.get("advisories"), "posts": s.get("openPosts") or [],
            "services": [{"name": x.get("name"), "status": x.get("status")} for x in s.get("services", [])]}


# ---------------------------------------------------------------- overview summary + feed
def build_summary(platforms, t):
    tot = len(platforms)
    ok = sum(1 for p in platforms if p["overall"] == "healthy")
    issues = [p["name"] for p in platforms if p["overall"] in ("degraded", "outage")]
    checks = [c for p in platforms for c in (p.get("checks") or [])]
    ms = [c["ms"] for c in checks if c.get("ok") and c.get("ms")]
    ice_items = [i for p in platforms if p["id"] == "ice" for c in p.get("components", []) for i in c.get("items", [])]
    maint = sorted([dict(m, platform=p["name"]) for p in platforms for m in (p.get("maintenance") or [])],
                   key=lambda m: m["start"])
    m365 = next((p for p in platforms if p["id"] == "m365"), {})
    m_svcs = [x for x in (m365.get("services") or []) if x.get("status") != "unknown"]
    changes = sorted([dict(h, platform=p["name"]) for p in platforms for h in (p.get("history") or [])],
                     key=lambda h: h["at"], reverse=True)

    feed = []
    for h in changes[:10]:
        feed.append({"at": h["at"], "platform": h["platform"], "kind": "change",
                     "text": f"Status changed: {LABEL.get(h['from'], h['from'])} → {LABEL.get(h['to'], h['to'])}"})
    for po in (m365.get("posts") or []):
        feed.append({"at": po.get("at"), "platform": "Microsoft 365", "kind": po.get("kind"),
                     "text": f"{po.get('id')} · {po.get('service')}: {po.get('title')}"})
    for m in maint:
        feed.append({"at": m["start"], "platform": m["platform"], "kind": "maintenance",
                     "text": ("In progress: " if m.get("active") else "Scheduled: ") + m["subject"],
                     "end": m.get("end")})
    for p in platforms:
        if p.get("manual"):
            feed.append({"at": p.get("since"), "platform": p["name"], "kind": "manual",
                         "text": "Set by hand: " + (p["manual"].get("note") or p["manual"]["status"])})
    if m365.get("lastIncident"):
        feed.append({"at": m365["lastIncident"], "platform": "Microsoft 365", "kind": "resolved",
                     "text": "Nov 12–14 global outage (EX1550543 / MO1550821) resolved."})
    feed = [f for f in feed if f.get("at")]
    feed.sort(key=lambda f: f["at"], reverse=True)

    upcoming = [m for m in maint if not m.get("active")]
    return {
        "platformsTotal": tot, "platformsOk": ok, "issues": issues,
        "checksTotal": len(checks), "checksOk": sum(1 for c in checks if c.get("ok")),
        "avgMs": int(sum(ms) / len(ms)) if ms else None,
        "iceTotal": len(ice_items), "iceOk": sum(1 for i in ice_items if i["status"] == "healthy"),
        "m365Services": len(m_svcs), "m365Ok": sum(1 for x in m_svcs if x.get("status") == "healthy"),
        "m365Advisories": m365.get("advisories"),
        "maintActive": sum(1 for m in maint if m.get("active")),
        "maintUpcoming": len(upcoming),
        "nextMaint": ({"platform": upcoming[0]["platform"], "subject": upcoming[0]["subject"],
                       "start": upcoming[0]["start"], "end": upcoming[0].get("end")} if upcoming else None),
        "lastChange": changes[0] if changes else None,
        "lastM365Incident": m365.get("lastIncident"),
        "feed": feed[:15],
    }


# ---------------------------------------------------------------- main
def strip_volatile(d):
    d = json.loads(json.dumps(d))
    d.pop("lastChecked", None)
    d.pop("nextCheck", None)
    d.pop("summary", None)
    for p in d.get("platforms", []):
        for c in p.get("checks", []) or []:
            c.pop("ms", None)
    return d


def main():
    with open(CONFIG) as f:
        cfg = json.load(f)
    try:
        with open(MANUAL) as f:
            manual = json.load(f)
    except Exception:
        manual = {}
    try:
        with open(OUT) as f:
            prev = json.load(f)
    except Exception:
        prev = {}
    prev_by_id = {p["id"]: p for p in prev.get("platforms", [])}

    out_platforms = []

    m = build_m365()
    m.update({"id": "m365", "name": "Microsoft 365", "statusPage": "https://status.cloud.microsoft",
              "support": "Microsoft 365 admin center → Service health",
              "xLinks": [["@MSFT365Status (official)", "https://x.com/search?q=from%3AMSFT365Status&f=live"],
                         ["Live posts: Outlook / Teams down", "https://x.com/search?q=%28outlook%20OR%20teams%20OR%20%22microsoft%20365%22%29%20down&f=live"]]})
    m.update(m365_extra())
    out_platforms.append(m)

    for p in cfg["platforms"]:
        res = build_ice(p) if p.get("type") == "ice" else build_checks(p)
        res.update({"id": p["id"], "name": p["name"], "statusPage": p.get("statusPage"),
                    "support": p.get("support"), "about": p.get("about"), "xLinks": p.get("xLinks", []),
                    "statusLabel": p.get("statusLabel"), "links": p.get("links", [])})
        out_platforms.append(res)

    t = now()
    for p in out_platforms:
        man = active_manual(manual, p["id"])
        p["autoOverall"] = p["overall"]
        if man:
            p["overall"] = man["status"]
            p["manual"] = {"status": man["status"], "note": man.get("note", ""), "until": man.get("until")}
            if man.get("note"):
                p["note"] = man["note"]
        old = prev_by_id.get(p["id"]) or {}
        hist = list(old.get("history", []))
        if old.get("overall") and old.get("overall") != p["overall"]:
            hist.append({"at": iso(t), "from": old["overall"], "to": p["overall"]})
        p["history"] = hist[-50:]
        p["since"] = old.get("since") if old.get("overall") == p["overall"] and old.get("since") else iso(t)

    out = {
        "lastChecked": iso(t),
        "nextCheck": iso(t + timedelta(minutes=INTERVAL_MIN + 2)),
        "overall": worst([p["overall"] for p in out_platforms], "healthy"),
        "platforms": out_platforms,
    }
    out["summary"] = build_summary(out_platforms, t)

    changed = strip_volatile(out) != strip_volatile(prev)
    stale = True
    if prev.get("lastChecked"):
        last = datetime.fromisoformat(prev["lastChecked"].replace("Z", "+00:00"))
        stale = (t - last) > timedelta(minutes=HEARTBEAT_MIN)
    if changed or stale or os.environ.get("FORCE_WRITE"):
        with open(OUT, "w") as f:
            json.dump(out, f, indent=1)
        print("platforms.json written", "(changed)" if changed else "(heartbeat)")
    else:
        print("No meaningful change; platforms.json left as is.")
    for p in out_platforms:
        print(f"  {p['name']}: {p['overall']} — {p.get('note','')[:90]}")


if __name__ == "__main__":
    main()

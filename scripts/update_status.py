#!/usr/bin/env python3
"""Refresh status.json from Microsoft Graph Service Health (no Claude needed).

Runs in GitHub Actions on a schedule. Needs three repository secrets:
  MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET
for an Entra app registration with the Microsoft Graph *application*
permission ServiceHealth.Read.All (admin consent granted).

Optional repository variable:
  ISSUE_IDS  comma-separated incident IDs to track (default: EX1550543,MO1550821).
             Leave it set to "auto" to track every open Exchange/M365-suite incident.
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

STATUS_FILE = os.path.join(os.path.dirname(__file__), "..", "status.json")
GRAPH = "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement"
INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", "15"))

# Page service rows -> Graph service names they cover (case-insensitive substring match)
SERVICE_MAP = {
    "Exchange Online / Outlook": ["exchange online"],
    "Microsoft Teams": ["microsoft teams"],
    "SharePoint Online / OneDrive": ["sharepoint online", "onedrive"],
    "Microsoft 365 Copilot": ["copilot"],
    "Microsoft Defender XDR": ["defender xdr", "microsoft defender", "defender for office"],
    "Planner / To Do / Power BI / Print": ["planner", "to do", "power bi", "universal print", "intune"],
    "M365 admin center / status page": ["microsoft 365 suite"],
}

# Graph status -> page status, plus a severity rank (higher = worse)
STATUS_MAP = {
    "serviceoperational": ("healthy", 0),
    "servicerestored": ("restored", 1),
    "postincidentreviewpublished": ("restored", 1),
    "resolved": ("resolved", 1),
    "resolvedexternal": ("resolved", 1),
    "mitigated": ("recovering", 2),
    "mitigatedexternal": ("recovering", 2),
    "falsepositive": ("healthy", 0),
    "investigationsuspended": ("healthy", 0),
    "verifyingservice": ("recovering", 2),
    "restoringservice": ("recovering", 2),
    "extendedrecovery": ("recovering", 2),
    "investigating": ("degraded", 3),
    "confirmed": ("degraded", 3),
    "reported": ("degraded", 3),
    "servicedegradation": ("degraded", 3),
    "serviceinterruption": ("outage", 4),
}
PLAIN = {
    "healthy": "Healthy per Microsoft Service Health.",
    "restored": "Microsoft reports service restored.",
    "resolved": "Microsoft reports this resolved.",
    "recovering": "Microsoft reports recovery in progress.",
    "degraded": "Microsoft reports service degradation.",
    "outage": "Microsoft reports a service interruption.",
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(ts):
    """Normalise Graph timestamps (may carry 7 fractional digits) to ...Z."""
    if not ts:
        return None
    ts = re.sub(r"\.\d+", "", ts).replace("+00:00", "Z")
    return ts if ts.endswith("Z") else ts + "Z"


def get_token():
    tenant = os.environ["MS_TENANT_ID"]
    body = urllib.parse.urlencode({
        "client_id": os.environ["MS_CLIENT_ID"],
        "client_secret": os.environ["MS_CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def graph(token, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def current_status(post_html):
    """Pull the 'Current status:' paragraph from a service-health post."""
    t = clean(post_html)
    m = re.search(r"Current status:\s*(.+?)(?:\s(?:Scope of impact|Start time|Estimated time to resolve|"
                  r"Root cause|Next update by|Preliminary root cause|End time|This is the final)\b|$)", t)
    s = (m.group(1) if m else t).strip()
    return s if len(s) <= 420 else s[:417].rsplit(" ", 1)[0] + "..."


def map_status(raw):
    return STATUS_MAP.get((raw or "").lower(), ("unknown", -1))


def main():
    with open(STATUS_FILE) as f:
        state = json.load(f)

    if state.get("resolved"):
        print("Already marked resolved; nothing to do.")
        return

    token = get_token()

    # 1) Incidents
    ids_cfg = (os.environ.get("ISSUE_IDS") or "EX1550543,MO1550821").strip()
    issues = []
    if ids_cfg.lower() == "auto":
        flt = urllib.parse.quote("isResolved eq false")
        data = graph(token, f"{GRAPH}/issues?$filter={flt}")
        issues = [i for i in data.get("value", [])
                  if i.get("classification") == "incident"
                  and any(k in (i.get("service") or "").lower() for k in ("exchange", "microsoft 365 suite"))]
    else:
        for iid in [x.strip() for x in ids_cfg.split(",") if x.strip()]:
            try:
                issues.append(graph(token, f"{GRAPH}/issues/{iid}"))
            except Exception as e:  # keep going if one ID is unavailable
                print(f"Could not fetch {iid}: {e}", file=sys.stderr)

    if not issues:
        print("No tracked issues returned; only refreshing lastChecked.")

    # 2) Timeline: add admin-center posts newer than the newest entry we already have
    tl = state.setdefault("timeline", [])
    newest = max((e["at"] for e in tl), default="1970-01-01T00:00:00Z")
    seen = {(e["at"][:16], e.get("source", "")) for e in tl}
    latest_post = None
    for iss in issues:
        src = f"Microsoft 365 admin center ({iss.get('id')})"
        for p in iss.get("posts") or []:
            at = iso(p.get("createdDateTime"))
            text = current_status((p.get("description") or {}).get("content", ""))
            if not at or not text:
                continue
            if latest_post is None or at > latest_post[0]:
                latest_post = (at, text)
            if at > newest and (at[:16], src) not in seen:
                tl.append({"at": at, "text": text, "source": src})
                seen.add((at[:16], src))
    tl.sort(key=lambda e: e["at"])

    # 3) Overall status from the tracked incidents
    if issues:
        if all(i.get("isResolved") for i in issues):
            state["overall"] = "resolved"
            state["resolved"] = True
            ends = [iso(i.get("endDateTime")) for i in issues if i.get("endDateTime")]
            state["resolvedAt"] = max(ends) if ends else now_iso()
            state["overallNote"] = ("Microsoft has marked the incident resolved. "
                                    + (latest_post[1] if latest_post else ""))[:600]
        else:
            worst = max((map_status(i.get("status")) for i in issues if not i.get("isResolved")),
                        key=lambda x: x[1])
            state["overall"] = worst[0] if worst[0] != "unknown" else state.get("overall", "degraded")
            if latest_post:
                state["overallNote"] = latest_post[1]

    # 4) Per-service status from the health overview
    try:
        overviews = graph(token, f"{GRAPH}/healthOverviews").get("value", [])
        for row in state.get("services", []):
            keys = SERVICE_MAP.get(row["name"])
            if not keys:
                continue
            hits = [map_status(o.get("status")) for o in overviews
                    if any(k in (o.get("service") or "").lower() for k in keys)]
            hits = [h for h in hits if h[1] >= 0]
            if hits:
                st = max(hits, key=lambda x: x[1])[0]
                if row.get("status") != st:
                    row["status"] = st
                    row["note"] = PLAIN.get(st, row.get("note", ""))
    except Exception as e:
        print(f"healthOverviews failed: {e}", file=sys.stderr)

    # 5) Timestamps
    state["lastChecked"] = now_iso()
    if state.get("resolved"):
        state.pop("nextCheck", None)
    else:
        nxt = datetime.now(timezone.utc) + timedelta(minutes=INTERVAL_MIN + 2)
        state["nextCheck"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(STATUS_FILE, "w") as f:
        json.dump(state, f)
    print(f"overall={state.get('overall')} resolved={state.get('resolved')} "
          f"timeline={len(tl)} lastChecked={state['lastChecked']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Microsoft 365 tab: refresh status.json from Microsoft Graph Service Health.

Auth (pick one):
  * Federated credential (recommended, no secret stored anywhere): the GitHub job
    gets a short-lived token from GitHub and swaps it with Entra. Needs repo
    variables MS_TENANT_ID and MS_CLIENT_ID and `permissions: id-token: write`.
  * Client secret: also set MS_CLIENT_SECRET (as a GitHub secret).

The Entra app needs the Microsoft Graph *application* permission
ServiceHealth.Read.All with admin consent. Read-only.

What it does every run:
  * Service tiles  <- current health of each Microsoft 365 service.
  * Open incidents <- any unresolved *incident* on the services SCE uses.
      - A new set of incidents starts a fresh incident view (the old one is archived
        under archive/).
      - When all tracked incidents are resolved, the tab shows Resolved.
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
STATUS_FILE = os.path.join(ROOT, "status.json")
ARCHIVE_DIR = os.path.join(ROOT, "archive")
GRAPH = "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement"
INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", "15"))

# Page service tiles -> Graph service names they cover (case-insensitive substring match)
SERVICE_MAP = {
    "Exchange Online / Outlook": ["exchange online"],
    "Microsoft Teams": ["microsoft teams"],
    "SharePoint Online / OneDrive": ["sharepoint online", "onedrive"],
    "Microsoft 365 Copilot": ["copilot"],
    "Microsoft Defender XDR": ["defender xdr", "microsoft defender", "defender for office"],
    "Planner / To Do / Power BI / Print": ["planner", "to do", "power bi", "universal print"],
    "Intune": ["intune"],
    "M365 admin center / status page": ["microsoft 365 suite"],
}
WATCHED = [k for keys in SERVICE_MAP.values() for k in keys]

STATUS_MAP = {
    "serviceoperational": ("healthy", 0),
    "servicerestored": ("restored", 1),
    "postincidentreviewpublished": ("restored", 1),
    "resolved": ("resolved", 1),
    "resolvedexternal": ("resolved", 1),
    "falsepositive": ("healthy", 0),
    "investigationsuspended": ("healthy", 0),
    "mitigated": ("recovering", 2),
    "mitigatedexternal": ("recovering", 2),
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
    "healthy": "Operational per Microsoft Service Health.",
    "restored": "Microsoft reports service restored.",
    "resolved": "Microsoft reports this resolved.",
    "recovering": "Microsoft reports recovery in progress.",
    "degraded": "Microsoft reports degradation or an advisory.",
    "outage": "Microsoft reports a service interruption.",
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(ts):
    if not ts:
        return None
    ts = re.sub(r"\.\d+", "", ts).replace("+00:00", "Z")
    return ts if ts.endswith("Z") else ts + "Z"


def _post(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def github_oidc_token():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=api://AzureADTokenExchange"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["value"]


def get_token():
    tenant = os.environ["MS_TENANT_ID"]
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    fields = {"client_id": os.environ["MS_CLIENT_ID"],
              "scope": "https://graph.microsoft.com/.default",
              "grant_type": "client_credentials"}
    if os.environ.get("MS_CLIENT_SECRET"):
        fields["client_secret"] = os.environ["MS_CLIENT_SECRET"]
    elif os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"):
        fields["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        fields["client_assertion"] = github_oidc_token()
    else:
        sys.exit("No credential: set MS_CLIENT_SECRET or run in GitHub Actions with id-token: write.")
    return _post(url, fields)["access_token"]


def graph(token, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def current_status(post_html):
    t = clean(post_html)
    m = re.search(r"Current status:\s*(.+?)(?:\s(?:Scope of impact|Start time|Estimated time to resolve|"
                  r"Root cause|Next update by|Preliminary root cause|End time|This is the final)\b|$)", t)
    s = (m.group(1) if m else t).strip()
    return s if len(s) <= 420 else s[:417].rsplit(" ", 1)[0] + "..."


def map_status(raw):
    return STATUS_MAP.get((raw or "").lower(), ("unknown", -1))


def watched(service):
    s = (service or "").lower()
    return any(k in s for k in WATCHED)


def archive(state):
    ids = state.get("incidentIds") or ["EX1550543", "MO1550821"]
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, "-".join(ids) + ".json")
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(state, f)


def main():
    with open(STATUS_FILE) as f:
        state = json.load(f)
    before = json.dumps({k: v for k, v in state.items() if k not in ("lastChecked", "nextCheck")}, sort_keys=True)
    token = get_token()

    # ---- service tiles: always current
    try:
        overviews = graph(token, f"{GRAPH}/healthOverviews").get("value", [])
        rows = {r["name"]: r for r in state.get("services", [])}
        for name, keys in SERVICE_MAP.items():
            hits = [map_status(o.get("status")) for o in overviews
                    if any(k in (o.get("service") or "").lower() for k in keys)]
            hits = [h for h in hits if h[1] >= 0]
            if not hits:
                continue
            st = max(hits, key=lambda x: x[1])[0]
            row = rows.get(name)
            if not row:
                row = {"name": name}
                state.setdefault("services", []).append(row)
            row["status"], row["note"] = st, PLAIN.get(st, "")
    except Exception as e:
        print(f"healthOverviews failed: {e}", file=sys.stderr)

    # ---- open incidents on watched services
    flt = urllib.parse.quote("isResolved eq false")
    open_issues = [i for i in graph(token, f"{GRAPH}/issues?$filter={flt}").get("value", [])
                   if i.get("classification") == "incident" and watched(i.get("service"))]
    open_ids = sorted(i["id"] for i in open_issues)
    tracked = state.get("incidentIds") or []

    if open_ids and set(open_ids) != set(tracked) and (state.get("resolved") or not set(open_ids) & set(tracked)):
        # new incident(s): archive the old view and start fresh
        archive(state)
        starts = [iso(i.get("startDateTime")) for i in open_issues if i.get("startDateTime")]
        state.update({"incidentIds": open_ids, "resolved": False, "incidentStart": min(starts) if starts else now_iso(),
                      "timeline": [], "downdetector": []})
        state.pop("resolvedAt", None)
    elif open_ids:
        state["incidentIds"] = sorted(set(tracked) | set(open_ids))

    issues = open_issues
    if not state.get("resolved") and state.get("incidentIds"):
        have = {i["id"] for i in issues}
        for iid in state["incidentIds"]:
            if iid not in have:
                try:
                    issues.append(graph(token, f"{GRAPH}/issues/{iid}"))
                except Exception as e:
                    print(f"Could not fetch {iid}: {e}", file=sys.stderr)

    if not state.get("resolved") and issues:
        tl = state.setdefault("timeline", [])
        newest = max((e["at"] for e in tl), default="1970-01-01T00:00:00Z")
        seen = {(e["at"][:16], e.get("source", "")) for e in tl}
        latest = None
        for iss in issues:
            src = f"Microsoft 365 admin center ({iss.get('id')})"
            for p in iss.get("posts") or []:
                at = iso(p.get("createdDateTime"))
                text = current_status((p.get("description") or {}).get("content", ""))
                if not at or not text:
                    continue
                if latest is None or at > latest[0]:
                    latest = (at, text)
                if at > newest and (at[:16], src) not in seen:
                    tl.append({"at": at, "text": text, "source": src})
                    seen.add((at[:16], src))
        tl.sort(key=lambda e: e["at"])
        if all(i.get("isResolved") for i in issues):
            ends = [iso(i.get("endDateTime")) for i in issues if i.get("endDateTime")]
            state.update({"resolved": True, "overall": "resolved", "resolvedAt": max(ends) if ends else now_iso()})
            state["overallNote"] = "Microsoft has marked the incident resolved. " + (latest[1] if latest else "")
        else:
            worst = max((map_status(i.get("status")) for i in issues if not i.get("isResolved")), key=lambda x: x[1])
            state["overall"] = worst[0] if worst[0] != "unknown" else "degraded"
            if latest:
                state["overallNote"] = latest[1]
            titles = "; ".join(f"{i['id']}: {i.get('title','')}" for i in issues if not i.get("isResolved"))
            state["incidentTitles"] = titles[:400]

    after = json.dumps({k: v for k, v in state.items() if k not in ("lastChecked", "nextCheck")}, sort_keys=True)
    try:
        last = datetime.fromisoformat(state.get("lastChecked", "").replace("Z", "+00:00"))
        stale = datetime.now(timezone.utc) - last > timedelta(minutes=60)
    except ValueError:
        stale = True
    if before == after and not stale and not os.environ.get("FORCE_WRITE"):
        print("No change; status.json left as is.")
        return
    state["lastChecked"] = now_iso()
    if state.get("resolved"):
        state.pop("nextCheck", None)
    else:
        state["nextCheck"] = (datetime.now(timezone.utc) + timedelta(minutes=INTERVAL_MIN + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(STATUS_FILE, "w") as f:
        json.dump(state, f)
    print(f"open incidents={open_ids} overall={state.get('overall')} resolved={state.get('resolved')}")


if __name__ == "__main__":
    main()

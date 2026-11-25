# Microsoft 365 Outage — Live Status

A simple live status page for the Microsoft 365 outage of November 12, 2026 (incidents EX1550543 / MO1550821), covering Exchange Online/Outlook, Teams, SharePoint/OneDrive, Copilot, Defender XDR, the admin center and Azure.

- `index.html` — the page; it reloads `status.json` every 60 seconds.
- `status.json` — current status, per-service status, Downdetector report counts and a timeline of Microsoft's updates. Updated about every 5 minutes until Microsoft declares the incident resolved.

This is an unofficial tracker compiled from Microsoft's public status posts and news coverage. For your own tenant, check **Service health** in the Microsoft 365 admin center.

## Auto-updates (GitHub Actions)

`.github/workflows/update-status.yml` runs every 15 minutes and calls `scripts/update_status.py`,
which reads Microsoft 365 Service Health from Microsoft Graph and updates `status.json`.

Setup (one time):
1. Entra admin center → App registrations → New registration (e.g. "m365-status-bot").
2. API permissions → Microsoft Graph → **Application** → `ServiceHealth.Read.All` → Grant admin consent.
3. Certificates & secrets → New client secret.
4. GitHub repo → Settings → Secrets and variables → Actions → add secrets
   `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`.
5. Optional variable `ISSUE_IDS` (default `EX1550543,MO1550821`; set `auto` to track any open Exchange/M365 incident).
6. Actions tab → "Update M365 status" → Run workflow.

When every tracked incident is resolved, the page shows Resolved and the job stops changing the file.


## Multi-platform dashboard

Tabs: Overview, Microsoft 365, ICE, Bloomberg, Cloud9 Trader, KRM22. `scripts/check_platforms.py` writes `platforms.json` every 15 minutes.

- **ICE**: read automatically from the public ICE Status Center feed. Which ICE systems count as WebICE, ICE Chat, ICE Connect etc. is set in `platforms.config.json`.
- **Bloomberg, Cloud9, KRM22**: no public status feeds, so these tabs show "is the website answering" checks. Add your own portal URLs under `checks` in `platforms.config.json`.
- **Manual status**: edit `manual.json` on GitHub (e.g. `"bloomberg": {"status": "degraded", "note": "Terminal slow per desk", "until": "2026-11-25T23:00:00Z"}`). The page updates within a couple of minutes. Set `status` back to `null` to clear it.

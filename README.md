# Microsoft 365 Outage — Live Status

A simple live status page for the Microsoft 365 outage of November 12, 2026 (incidents EX1550543 / MO1550821), covering Exchange Online/Outlook, Teams, SharePoint/OneDrive, Copilot, Defender XDR, the admin center and Azure.

- `index.html` — the page; it reloads `status.json` every 60 seconds.
- `status.json` — current status, per-service status, Downdetector report counts and a timeline of Microsoft's updates. Updated about every 5 minutes until Microsoft declares the incident resolved.

This is an unofficial tracker compiled from Microsoft's public status posts and news coverage. For your own tenant, check **Service health** in the Microsoft 365 admin center.

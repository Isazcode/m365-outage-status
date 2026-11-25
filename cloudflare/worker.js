// SCE Platform Status - backup timer.
// Cloudflare Cron Trigger (every 15 min) -> asks GitHub to run the status-check job.
// Needs one Worker secret: GH_TOKEN = fine-grained GitHub token, repo Isazcode/m365-outage-status only, Actions: read & write.
const URL = "https://api.github.com/repos/Isazcode/m365-outage-status/actions/workflows/status-check.yml/dispatches";

async function runJob(env) {
  const r = await fetch(URL, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "sce-status-timer",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  const ok = r.status === 204;
  console.log(`GitHub dispatch: HTTP ${r.status}${ok ? "" : " " + (await r.text()).slice(0, 200)}`);
  return ok;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runJob(env));
  },
  // Visiting the Worker URL just reports that it's alive (it does not run the job, so nobody can spam it).
  async fetch() {
    return new Response("SCE status timer is running. It starts the GitHub status check every 15 minutes.", {
      headers: { "content-type": "text/plain" },
    });
  },
};

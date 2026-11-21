/**
 * Search Console bridge for ReachPilot (findable.py).
 *
 * Google answers questions about its index only to the signed-in owner of a site. This script runs as you, in
 * your own Google account, and answers two questions for ReachPilot: is this page in Google's index, and
 * please read this sitemap. It can do nothing else, and it answers only to someone who knows TOKEN.
 *
 * Set up once (script.google.com, signed in as the account that owns the site in Search Console):
 *   1. New project, paste this file. In Project Settings tick "Show appsscript.json" and give it:
 *        "oauthScopes": ["https://www.googleapis.com/auth/webmasters",
 *                        "https://www.googleapis.com/auth/script.external_request"]
 *   2. Project Settings > Script Properties: TOKEN = a long random text of your own.
 *   3. Deploy > New deployment > Web app, "Execute as: Me", "Who has access: Anyone". Approve the sign-in.
 *   4. In ReachPilot's secrets.json: "search_console": {"bridge_url": "<the /exec address>", "token": "<TOKEN>"}
 */
function doPost(event) {
  const answer = (data) => ContentService.createTextOutput(JSON.stringify(data)).setMimeType(ContentService.MimeType.JSON);
  const token = PropertiesService.getScriptProperties().getProperty("TOKEN");
  let ask;
  try {
    ask = JSON.parse(event.postData.contents);
  } catch (error) {
    return answer({ error: "not JSON" });
  }
  if (!token || ask.token !== token) return answer({ error: "wrong token" });
  const headers = { Authorization: "Bearer " + ScriptApp.getOAuthToken() };
  if (ask.action === "inspect") {
    const reply = UrlFetchApp.fetch("https://searchconsole.googleapis.com/v1/urlInspection/index:inspect", {
      method: "post", contentType: "application/json", headers: headers, muteHttpExceptions: true,
      payload: JSON.stringify({ inspectionUrl: ask.url, siteUrl: ask.property }),
    });
    if (reply.getResponseCode() !== 200) return answer({ error: "Search Console answered " + reply.getResponseCode() });
    const index = (JSON.parse(reply.getContentText()).inspectionResult || {}).indexStatusResult || {};
    return answer({ verdict: index.verdict || "", coverage: index.coverageState || "", crawled: index.lastCrawlTime || "" });
  }
  if (ask.action === "sitemap") {
    const address = "https://www.googleapis.com/webmasters/v3/sites/" + encodeURIComponent(ask.property)
      + "/sitemaps/" + encodeURIComponent(ask.url);
    const reply = UrlFetchApp.fetch(address, { method: "put", headers: headers, muteHttpExceptions: true });
    return reply.getResponseCode() < 300 ? answer({ ok: true }) : answer({ error: "Search Console answered " + reply.getResponseCode() });
  }
  return answer({ error: "unknown action" });
}

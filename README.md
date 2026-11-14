<p align="center">
  <a href="https://github.com/juw12/reachpilot"><img src="docs/media/banner.jpg" alt="ReachPilot: turns your GitHub into a LinkedIn schedule." width="100%"></a>
</p>

<p align="center">
  <b>Turns your GitHub into a posting schedule.</b><br>
  For developers who ship side projects but never get round to posting them. ReachPilot writes each post from
  your repo's real facts, adds a short demo video of the product being used (or a fresh screenshot), posts it
  through LinkedIn's official API, cuts it to fit Bluesky,
  Mastodon, Dev.to, and X, Threads and Substack (free through Buffer), and learns which projects people click.
</p>

<p align="center">
  <a href="https://github.com/juw12/reachpilot/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/juw12/reachpilot/tests.yml?branch=main&label=tests" alt="Tests"></a>
  <img src="https://img.shields.io/badge/Windows-24292f" alt="Windows">
  <img src="https://img.shields.io/badge/Python_3.10+-24292f" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-2563eb" alt="MIT">
</p>

<p align="center"><a href="https://juw12.github.io/reachpilot/"><b>Try the live demo</b></a> (the real dashboard with made-up data)</p>

<p align="center"><img src="docs/media/dashboard.png" alt="ReachPilot dashboard, demo data: a short list of the things that need a human, each with one button." width="100%"></p>

**Safe by design:** official Share on LinkedIn API only (no scraping, no browser bots, no auto-likes), every number in a
post must exist in your repo, and everything (tokens, history, drafts) stays on your PC in git-ignored files.

## Why

I build a lot of side projects. Writing a LinkedIn post for each one, finding a screenshot, picking a time, then doing
it again next week for the older ones: that's the part that never happens. ReachPilot does it for me, without making anything up.

## What it does

Every 30 minutes, Windows Task Scheduler runs one pass:

1. **Watches GitHub** through the `gh` CLI: new public repos, new releases, real commit activity.
2. **Picks what to post** in the next slot (Tue/Wed/Thu 08:00 by default), in this order:
   new repo → new release → your hand-written queue → active-repo update → top-performer roundup → an older project from a fresh angle.
3. **Writes the post from facts only**: README tagline and feature bullets, description, topics, commit messages.
   A guard rejects any number that isn't in the source. No invented users, stars or backstory.
4. **Adds real visuals**: a short demo video of the product being used, recorded fresh from the live site
   (8 to 22 seconds: it looks, presses a few controls, scrolls through and comes back up), posted natively on
   LinkedIn and every platform that takes video. The recording only reads the site: every request that is not
   a GET is blocked and sign-in, pay, delete and download buttons are never pressed. Projects without a live
   site, and any platform that refuses the video, get a fresh screenshot of the site (and the repo page) plus
   media from the README instead. Switch the video off with `"demo_video": false` in `config.json`.
5. **Never repeats itself**: four angles (launch, one-feature deep dive, behind the build, ask for feedback),
   different README facts each time, a 21-day cooldown per project and a similarity cap against every past post.
   If nothing fresh qualifies, it skips the slot instead of reposting.
6. **Learns what lands**:
   - visitors GitHub attributes to linkedin.com / lnkd.in (repo traffic API),
   - stars gained in the week after a post,
   - "Try it" clicks: site links go through `<you>.github.io/go/<project>/`, a tiny page that counts the click with
     [GoatCounter](https://www.goatcounter.com) and forwards in well under a second.

   Projects and angles are then chosen with Thompson sampling: winners get posted more, untested ones still get a turn.

It also repairs garbled characters in READMEs (text saved in the wrong encoding), skips install/run steps when picking
feature bullets, and puts links in the first comment.

### Other platforms (free, official APIs)

Every LinkedIn slot also goes to the platforms you connect, each cut to fit from the same fact-checked post
(a platform can shorten a post, never add a claim):

| Platform | What it gets | You need |
|---|---|---|
| Bluesky | short post (300 characters), the demo video (or up to 4 screenshots), clickable link | your password once (setup turns it into an app password and stores only that) |
| Mastodon | short post (500 characters), the demo video (or screenshots) | an access token from your instance |
| Dev.to | an article for launches and releases (max one per project every 60 days), canonical URL back to you | an API key |
| Telegram | the full post with the demo video (or the first screenshot), in your own channel | a bot token + channel |
| Discord | the full post with the demo video (or the first screenshot), in your own server | a webhook URL |
| X | short post (280 characters as X counts them), the demo video (or the project's image), one link | a free Buffer account with your X connected, and a Buffer API key (Buffer publishes it, so no X API fees) |
| Threads | short post (500 characters), one topic tag, the demo video (or the project's image), one link | Threads connected in the same Buffer account (same API key) |
| Substack | a Substack Note with the post and links, the project's image | Substack connected in the same Buffer account (same API key) |

Each platform publishes and retries on its own (3 attempts within 3 days), so one platform failing never blocks
LinkedIn or the others. `preview` shows every platform's version.

X's own API now charges per post, so ReachPilot posts to X through [Buffer](https://buffer.com)'s free plan
and public API instead: Buffer is an official X partner and publishes the post to your account straight away,
at no cost. Buffer can't take local files, so the demo video is published at
`https://<you>.github.io/demo/<repo>.mp4` (your GitHub Pages repo, replaced at most every 60 days) and Buffer
fetches it from there for X and Threads. When that link isn't live yet, or Buffer refuses the video, the post
carries the README's first image (or GitHub's card for the repo). If you'd rather use the paid X API directly, type `api` at the setup prompt; those posts are capped
by `x_monthly_budget_usd` in `config.json` (about $0.20 per post with a link; default $5 a month).

Once a day `growth.py` grows the audience and points all of it at GitHub:

- **Profiles:** Mastodon bio plus link fields (GitHub, portfolio, Dev.to, Bluesky), featured hashtags and
  "discoverable"; Bluesky bio. Written from `profile` in `config.json`, and only pushed again when that block
  changes, so edits you make in the apps stay.
- **Pinned intro:** the first post on Mastodon and Bluesky is pinned to the profile.
- **Follow back:** new followers on Mastodon and Bluesky, at most 25 a day each, bots skipped.
- **GitHub profile:** your Mastodon, Bluesky and Dev.to accounts are listed on your GitHub profile (`socials` in
  the `profile` block), using the GitHub login Git already stores. GitHub marks those links `rel="me"`, which
  gives the GitHub link on your Mastodon profile a verified tick.
- **Measured:** posts per platform, follower counts (Mastodon, Bluesky, Dev.to, GitHub), and the GitHub visitors
  that came from LinkedIn, Mastodon, Bluesky, Dev.to, X, Threads, Substack, Telegram and Discord (repo traffic API).
  All show on the dashboard's "Every platform" table.
- **Recorded:** every change it makes is written to `growth_log` in `state.db` and counted on the dashboard
  ("Growth actions"), with details in the log file.

It never follows, likes or replies to strangers automatically: Mastodon instances and Bluesky treat that as spam.

## What it deliberately doesn't do

- No browser automation, scraping, auto-likes or auto-comments on other people's posts. Only the official
  **Share on LinkedIn** API, so the account stays in good standing.
- No AI-invented claims. An optional local model (Ollama) can rephrase a post, but its output is rejected if it adds numbers.
- No silent failures: a LinkedIn login that is about to expire, a failed post or a draft waiting for review shows up
  on the dashboard's to-do list, and you get a Windows notification a week before the 60-day LinkedIn token runs out.

## Setup

You need Windows 10 or 11, Python 3.10+, Microsoft Edge (used for screenshots and the demo recording, so no browser download; `pip` brings its own
ffmpeg through `imageio-ffmpeg`),
the [GitHub CLI](https://cli.github.com/) and a free LinkedIn developer app.

```powershell
git clone https://github.com/juw12/reachpilot; cd reachpilot
pip install -r requirements.txt
notepad config.json                  # your GitHub user, time zone, posting slots, weekly cap, excluded repos
gh auth login                        # GitHub CLI: repos, READMEs and traffic
python autopilot.py init             # baseline your repos (+ optional seed_queue.json of hand-written posts)
python autopilot.py auth             # LinkedIn app with "Share on LinkedIn" + "Sign In with LinkedIn (OpenID)",
                                     # redirect URL http://localhost:8765/callback
python autopilot.py preview 5        # first result: the next 5 posts as drafts in drafts/, nothing is posted
python autopilot.py channels         # optional: connect Bluesky, Mastodon, Dev.to, Telegram, Discord, X (each verified)
python autopilot.py goatcounter      # optional: free GoatCounter site + token for "Try it" clicks
python autopilot.py install          # scheduled task \ReachPilot\LinkedIn posts: every 30 min + at sign-in
python selfupdate.py install         # optional: task \ReachPilot\Updates keeps the code current (fast-forward only)
```

In `config.json`, `"mode": "auto"` posts by itself; set `"mode": "review"` to hold each draft until you run `approve`.

### Create the LinkedIn app (once, about 5 minutes)

ReachPilot posts as you through your own free LinkedIn developer app; nobody else's server is involved.

1. Open [linkedin.com/developers/apps](https://www.linkedin.com/developers/apps) and press **Create app**. LinkedIn requires
   every app to be linked to a LinkedIn Page, so pick (or create) a Page you manage, then add an app name and logo.
2. On the **Products** tab, add **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**. Both are self-serve.
3. On the **Auth** tab, under *Authorized redirect URLs*, add `http://localhost:8765/callback` exactly.
4. Keep the Auth tab open: `python autopilot.py auth` asks for the **Client ID** and **Primary Client Secret** shown there,
   saves them to `secrets.json` (git-ignored), and opens LinkedIn so you can approve the scopes `openid profile w_member_social`.

The access token lasts 60 days and self-serve apps get no refresh token, so you sign in again every two months;
the dashboard and a Windows notification remind you a week before.

### What talks to what

| Service | Why | What is sent |
|---|---|---|
| GitHub | find repos, READMEs, commits, traffic | read-only requests through `gh` (public API as fallback) |
| LinkedIn | publish and measure posts | the post text, the demo video or images, and the first comment with links |
| Bluesky, Mastodon, Dev.to, Telegram, Discord (only those you connect) | publish the cut-down post | the post text, the demo video or screenshots, and links |
| Buffer (X, Threads, Substack, only those you connect) | publishes the cut-down post right away | the post text, the demo video URL or project image URL, and links |
| GoatCounter (optional) | count "Try it" clicks | a page view when someone opens a tracked go-link |
| Ollama (optional, local) | rephrase a post | nothing leaves your PC |

There is no ReachPilot server and no telemetry.

## Everyday use

| Command | What it does |
|---|---|
| `dashboard` | opens the local app (127.0.0.1 only): to-do list, schedule with post previews, every post's reach with links to it on each platform, followers and GitHub visitors per platform, what it changed on your accounts, GitHub visitors, stars, "Try it" clicks, what the bot has learned, and the log. It closes itself after 15 idle minutes. |
| `status` | token expiry, queue, recent posts, best performers, click counts |
| `preview 5` | write the next 5 posts to `drafts/` without posting |
| `approve` | in review mode, approve the waiting draft |
| `uninstall` | remove the `\ReachPilot\LinkedIn posts` task |

**Pause:** create an empty file named `PAUSE` in the ReachPilot folder (or press Pause on the dashboard); delete it to resume.
**Remove completely:** `python autopilot.py uninstall`, then
`Unregister-ScheduledTask -TaskPath '\ReachPilot\' -TaskName Updates` if you installed the updater, and delete the folder.

### The dashboard as a hub

The dashboard can also show other outreach automations. Personal ones (for example a job search) live in `modules/`,
which is git-ignored and never committed; the dashboard only shows a module that exists on disk. Every job runs from
Task Scheduler under `\ReachPilot\`, dashboard actions are whitelisted and need a per-session token from the same origin,
and the `?` button explains per module when it runs, what it does by itself and what it needs from you.

When the podcast module ships `promotion.py`, a **Podcast** tab tracks its promotion: which directories list the show
(with Mark as submitted for the ones that can't be checked), downloads per week and per episode from OP3, series compared,
where each episode was announced and the follower counts on those channels. The tab only reads files; when the download
numbers are older than 15 minutes it starts `promotion.py refresh` in the background.

## Files

| File | Role |
|---|---|
| `autopilot.py` | scheduler and commands |
| `composer.py` | post writing and the no-invented-facts guards |
| `github_source.py` | README and commit facts |
| `visuals.py` | screenshots and the demo video recording (Edge, headless) |
| `linkedin_api.py` | OAuth, images, video, posts, comments |
| `channels.py` | Bluesky, Mastodon, Dev.to, Telegram, Discord: per-platform cutting, publishing, retries |
| `growth.py` | daily: profiles, pinned intro, follow-back, follower counts |
| `analytics.py` | reach scoring |
| `trylinks.py` | tracked go-links, demo video hosting on GitHub Pages |
| `dashboard.py` + `dashboard.html` | local dashboard (restarts itself onto new code) |
| `hub.py` | dashboard modules, to-do list and whitelisted actions |
| `selfupdate.py` | fast-forward updater used by the dashboard and the Updates task |
| `selftest.py` | tests (run in CI) |

## License

MIT License, © 2026 Juwen Lie.

# google-health-cli

Download your **Fitbit / Google Health** data to your own machine, as JSON, with one command.

The old Fitbit Web API was shut down on **30 October 2026**: every script and MCP server
built on `api.fitbit.com` stopped working. The only way in is now the
[Google Health API](https://developers.google.com/health).

This script automates everything that **can** be automated and walks you through the three
things Google forces you to do by hand in the console.

- **No Python dependencies** — standard library only (3.9+)
- [`gcloud`](https://cloud.google.com/sdk/docs/install) is needed for the initial setup only
- Credentials stay on your machine, in `~/.config/google-health/config.json` (mode `600`)
- The guided setup speaks **English or Italian** — it asks on the first prompt

## Usage

```bash
git clone https://github.com/pavelux00x/google-fitbit-to-claudecode-codex.git
cd google-fitbit-to-claudecode-codex
python3 health.py setup     # once: about 10 minutes
python3 health.py fetch     # whenever you want fresh data
```

Data lands in `health-data/health-YYYY-MM-DD.json`.

```bash
python3 health.py fetch --days 30                # last 30 days
python3 health.py fetch --types steps sleep      # only some types
python3 health.py whoami                         # check the token works
python3 health.py selfcheck                      # offline test, no network
python3 health.py --lang it setup                # skip the language prompt
```

## What `setup` does

| Step | Who does it |
|---|---|
| Google login | `gcloud auth login` |
| Create the Cloud project | automatic |
| Enable `health.googleapis.com` | automatic |
| Google Auth Platform: first-time wizard (new projects) | **you**, page opened by the script |
| Audience: Testing + test user | **you**, page opened by the script |
| Data access: pick the scopes | **you**, page opened by the script |
| Create the OAuth client + redirect URI | **you**, page opened by the script |
| Check the OAuth client really exists | automatic, **without opening a browser** |
| OAuth login, consent, refresh token | automatic (loopback + PKCE) |
| Verify with `users.getIdentity` | automatic |

**Why three manual steps?** Google exposes no API for creating an OAuth client. The IAP
OAuth Admin API, the last one that could, was shut down on 19 March 2026. No script can
click those three buttons for you — anything claiming otherwise is using Terraform, which
still needs an existing OAuth client to authenticate itself.

If you run `setup` again while the OAuth client already exists — saved credentials, or a
`client_secret_*.json` in `~/Downloads` — the script verifies it against Google's token
endpoint and offers to skip the console steps instead of reopening four pages. The check
costs nothing: it asks for a refresh with a deliberately fake token and looks at whether
Google answers `invalid_grant` (client is fine) or `invalid_client` (client missing or
wrong secret).

## On a server

### Logging in on a headless VPS

The OAuth consent happens in a browser, and the browser has to come back to
`http://localhost:8765/callback`. On a VPS that link is useless if you open it on your
laptop: *your* localhost is not the server's, so the consent never reaches the script.

An SSH tunnel makes the two the same. From your own machine:

```bash
ssh -L 8765:localhost:8765 user@your-vps
```

Then, inside that session:

```bash
python3 health.py setup
```

The script notices it has no browser, prints the link instead of trying to open one, and
waits. Open that link on your laptop: the browser hits *your* `localhost:8765`, SSH carries
the connection to the VPS, and the script receives the consent. Nothing else changes — the
redirect URI stays `http://localhost:8765/callback`, so the OAuth client needs no extra
configuration.

Force that behaviour anywhere with `--no-browser`.

### Or just copy the config

If you would rather not tunnel, run `setup` on your laptop and copy the result across:

```bash
scp ~/.config/google-health/config.json user@server:~/.config/google-health/config.json
ssh user@server chmod 600 ~/.config/google-health/config.json
```

### Then keep it running

**First, remove the 7-day expiry**, or the server stops after a week: console → Branding
(fill the required fields) → Audience → *Publish app*. It stays an unverified app, capped
at 100 users, which is irrelevant if it is just you.

Then a systemd timer every 15 minutes:

```ini
# ~/.config/systemd/user/health.service
[Service]
Type=oneshot
WorkingDirectory=%h/google-fitbit-to-claudecode-codex
ExecStart=/usr/bin/python3 health.py fetch --days 2
```

```ini
# ~/.config/systemd/user/health.timer
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now health.timer
```

In cron it is a single line:

```
*/15 * * * * cd ~/google-fitbit-to-claudecode-codex && /usr/bin/python3 health.py fetch --days 2
```

### Why 15 minutes and not 5

Quota is not the constraint: the limit is 300 requests per minute per user and this script
makes 7 per run. The point is that **the data upstream is never fresher than that**. A
Fitbit syncs to the phone over Bluetooth roughly every 15 minutes, and only while the
Google Health app is open and the phone is in range. Polling every 5 minutes returns the
same payload three times in a row.

### If you really need real time: webhooks

The API can notify you when data changes instead of making you look for it. You register a
*subscriber* with a public HTTPS endpoint and Google POSTs to it on every update. The
honest cost of that road:

- a public HTTPS endpoint, TLS 1.2+, reachable from Google
- on registration Google sends two verification POSTs with body `{"type": "verification"}`:
  the one **with** the `Authorization` header must answer 200/201, the one **without** must
  answer 401/403. Get either wrong and the create fails with `FAILED_PRECONDITION`
- every notification is ECDSA P-256 signed, signature in the `GOOGLE-HEALTH-API-SIGNATURE`
  header, to be verified against the raw body. Keys rotate every 30 days, so the keyset must
  be fetched, not hardcoded
- **the notification carries no data**: it names the user, the data type and an interval
  only. You still fetch the values yourself with `dataPoints:reconcile`
- you must answer `204` immediately and process asynchronously. Anything else and Google
  retries with backoff, holding notifications for 7 days
- managing subscribers needs the `cloud-platform` scope, so a service account rather than
  the user token this script uses

## Dashboard

```bash
python3 health.py fetch --days 30
python3 dashboard.py --analyze --lang it
open dashboard.html
```

`dashboard.py` builds a single self-contained `dashboard.html`: daily steps, nights stacked
by sleep stage (deep / REM / light / awake), a handful of KPIs, and a blue woven Fitbit band in 3D
you can spin with the cursor — fabric texture, metal keeper, and the last twelve days
drawn on its little screen. The data is inlined in the file, so it opens offline with no
server — and for the same reason `dashboard.html` is git-ignored: it contains your health data.

`--analyze` sends a compact textual digest (never the raw JSON) to
[OpenRouter](https://openrouter.ai) and embeds the reply. The default model is
`openai/gpt-4o-mini`; pick another with `--model`. Beware that several cheap models are
reasoning models: with a small token budget they spend it all thinking and return empty
content, which the script reports instead of writing a blank panel. The answer is cached in
`health-data/analysis.json` and reused until you pass `--analyze` again.

The API key is read from a file on disk, never passed on the command line and never printed.
Edit `KEY_FILE` at the top of `dashboard.py` to point at yours.

Treat the analysis as a reading of the numbers, not medical advice.

## Limits worth knowing up front

- **Use the same Google account as your Fitbit.** Google Workspace accounts are not supported by Fitbit.
- **In Testing mode the refresh token expires after 7 days.** The script tells you when it
  happens: `python3 health.py setup --login-only`. To make it permanent, publish the app from
  the Audience page (it stays unverified, capped at 100 users — irrelevant for personal use).
- **There is no sandbox**: the project you create reads your real data. Every scope requested is `.readonly`.
- Some types cap a single call at 14 days (`heart-rate`, `active-minutes`, `total-calories`),
  90 for the rest. The script splits the requests for you.

## Data types downloaded

Via `dailyRollUp` (one value per day): `steps`, `distance`, `weight`, `active-minutes`,
`heart-rate`, `total-calories`. Via `list` (one record per session): `sleep`.

The full list of types lives in the
[discovery document](https://health.googleapis.com/$discovery/rest?version=v4).

## Troubleshooting

| Error | Almost certainly |
|---|---|
| `403` on a call | a scope was not selected under *Data access*, or your address is not in *Test users* |
| `invalid_grant` | refresh token expired (the 7 days) → `setup --login-only` |
| `redirect_uri_mismatch` | the OAuth client is missing exactly `http://localhost:8765/callback` |
| `enable` fails mentioning billing | link a billing account to the project and rerun with `--project NAME` |
| project creation fails | project quota is full: create one by hand and use `--project NAME` |

## License

MIT. Not affiliated with Google or Fitbit.

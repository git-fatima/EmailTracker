# Gmail → Discord alerts (GitHub Actions + Upstash Redis)

Each external cron trigger runs one quiet scan. The script GETs the Redis string
`config`, refreshes Gmail OAuth access, scans new mail, and posts matches to a
Discord webhook. Redis also stores progress. No state branch, Telegram bot,
SQLite file, Base64 config conversion, extra encryption layer or server required.

## Configuration

Copy config.example.json into an Upstash **String** key named **config**. Replace
every credential placeholder. Paste ordinary JSON as the value, not an extra
quoted JSON string. Do not set an expiry. Never commit a populated config file.

| Field | Value |
| --- | --- |
| gmail.client_id | Google OAuth client ID |
| gmail.client_secret | Google OAuth client secret |
| gmail.refresh_token | Obtained by authorizing Gmail read access once locally |
| discord.webhook_url | Complete copied incoming webhook URL |
| scan.keywords | Case-insensitive substrings; any match in sender, subject or body alerts |
| scan.initial_lookback_seconds | First-run history only; 86400 is one day |
| scan.overlap_seconds | Recheck margin; 300 adds five minutes |
| scan.include_spam_trash | Whether to include Spam/Trash |
| scan.exclude_labels | SENT/DRAFT excluded by default; [] includes them |

External cron controls the interval. Later scans start at the last completed
checkpoint and catch up after downtime. Editing config affects the next run.
Keyword edits do not reprocess IDs already seen. Config is read once per run.

## Discord setup

Use a normal server text channel. Open channel settings > Integrations > Webhooks
> New Webhook. Choose the channel, save, and Copy Webhook URL. Put that entire URL
in discord.webhook_url. No bot token, chat ID, channel ID or Discord login password
is needed. The copied URL is a credential; anyone holding it can post via it.

Alerts go to the channel, not a private DM. Channel members can see them. Set the
channel's notification preferences to All Messages if you want phone alerts.
Email sender/subject/keyword matches are included, but no body or attachment.
All mentions and automatic link embeds are disabled. Ordinary email Markdown
may still affect appearance. Forum/thread webhooks aren't supported in this v1.

## Gmail OAuth once locally

Enable Gmail API in Google Cloud. Configure Google Auth Platform, request
gmail.readonly, add your Gmail as a test user if External/Testing, and create a
Desktop app client. Download its JSON as credentials.json beside authorize.py.

```bash
python -m venv .venv
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe authorize.py
```

macOS/Linux:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python authorize.py
```

Approve access in the browser. Copy client_id, client_secret and refresh_token
from local state/token.json into the Redis config. No Gmail access token needs
storing; each run gets one. External/Testing Gmail refresh tokens expire after
seven days; address publishing status and reauthorize before long-term use.
Revocation/password changes may require authorization again too.

## GitHub setup

Upload this folder's CONTENTS to your repository root, including `.github`.
Replace the older workflow.py and workflow YAML. Old state_store.py/config.json
and Telegram settings are no longer used. Prefer a private repository. Keep only
these two repository Secrets (Settings > Secrets and variables > Actions):

```text
UPSTASH_REDIS_REST_URL
UPSTASH_REDIS_REST_TOKEN
```

Use Upstash's HTTPS REST endpoint and read/write REST token. These bootstrap
credentials stay outside Redis config, avoiding a circular dependency. No real
credentials are supplied in this package.

The workflow needs contents:read only. It caches pip package downloads, installs
dependencies on the fresh runner, then executes python workflow.py. It does not
cache credentials, Redis state or account data. Application stdout/stderr is
discarded. GitHub still retains platform logs, step names, commands, timings,
cache messages and pass/fail status. No custom application logging is enabled.

Run Actions > Gmail alerts > Run workflow once manually on your default branch.
Then have your external cron send:

```text
POST https://api.github.com/repos/OWNER/REPO/actions/workflows/gmail-alerts.yml/dispatches
Authorization: Bearer YOUR_GITHUB_DISPATCH_TOKEN
Accept: application/vnd.github+json
Content-Type: application/json

{"ref":"main"}
```

Use your actual default branch. Keep the workflow file on that branch. The cron
provider needs a fine-grained GitHub token restricted to this repo with Actions:
write; it does not need Gmail, Discord or Redis credentials. Dispatch acceptance
does not mean the scan succeeded. Monitor Actions status and token expiry.

## Redis state and recovery

| Key | Purpose |
| --- | --- |
| config | Your manually maintained JSON string with credentials and settings |
| gmail-alerts:state | Script-managed JSON string: account, checkpoint, seen IDs, pending alert metadata |
| gmail-alerts:lock | Temporary random lock; expires after 20 minutes |

No credential or state encryption is added at application level, as requested.
Use HTTPS. Treat Redis access as access to the Gmail/Discord credentials. Disable
eviction for this database where supported and keep config/state without expiry.
Missing state starts a fresh initial window; deletion/eviction loses prior history.
Never store a second account under these same keys. An account change with old
state fails; deliberately clear state only if you accept a fresh scan and possible
repeated alerts. Use separate databases or change key constants for other users.

The script saves processed IDs/pending metadata per message, then advances the
checkpoint after all pages. A partial scan retries without reprocessing committed
IDs. Queued alerts are retried on subsequent runs. A Redis lock and workflow
concurrency prevent overlapping normal runs; a killed runner can leave the lock
until its TTL expires. The Actions job has a 15-minute maximum.

Discord uses wait=true for delivery confirmation, disables mentions, and retries
short rate-limit responses. Other errors leave the alert pending. A crash or lost
Redis save after successful Discord posting can produce a duplicate. Exactly-once
delivery is not guaranteed. If Gmail authentication fails, no pending delivery is
attempted, to ensure queued data still belongs to the authorized account.

Seen IDs are retained indefinitely in one small JSON state document, suitable for
your low-volume test inbox. State is rewritten per message; Redis command count,
transfer and size grow over time. Migrate to Redis hashes/sets if volume grows.
The code parses email text/HTML, excluding named attachments; no OCR, PDF search,
linked-page reading or AI is used. Quoted old replies/signatures can match.
Permanently deleted messages and arbitrarily late-indexed old mail can be missed.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use fake responses; real Gmail/Discord/Upstash/GitHub execution requires your
credentials. No live messages were sent during creation. Check your Actions and
Upstash plan allowances; no additional infrastructure is provisioned here.

Official docs:

- https://docs.discord.com/developers/resources/webhook#execute-webhook
- https://upstash.com/docs/redis/features/restapi
- https://developers.google.com/workspace/gmail/api/quickstart/python

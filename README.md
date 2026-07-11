# Check Submission Status

[Chinese documentation](README_zh.md)

## Purpose

Check Submission Status signs in to one or more ScholarOne author dashboards, reads the
current manuscripts, records durable SQLite history, and sends changes through
[Apprise](https://appriseit.com/getting-started/universal-syntax/). Each valid dashboard row
provides the normalized short manuscript ID, complete title, displayed submission date and its
parsed date, and current status.

`main.py` performs one check and exits. It is not a daemon and contains no scheduler.

## Behavior

Each account has an independent history and at most one complete message per check:

- The first successful check for a new or reactivated account is a verification. Every present
  in-scope manuscript is reported as `CURRENT`; an empty scope still sends a verification message.
- Later checks report `NEW`, `STATUS_CHANGED`, `DISAPPEARED`, and `REAPPEARED` events. A
  disappearance is created only after a complete, valid dashboard was parsed and a previously
  accepted present manuscript is absent.
- Title-only and submission-date-only changes are stored as the newest complete snapshot but do
  not notify. A later notifying event uses that updated snapshot.
- All events for one account are sorted by manuscript ID and aggregated into one untruncated
  message. Accounts never share a notification.
- Every configured Apprise destination is attempted independently. If any destination succeeds,
  the notification and deferred state changes are committed. Failed destinations from that
  partially successful batch are not retried.
- If every destination fails, the check is recorded as failed and notification-bearing state is
  not accepted. The next one-shot invocation regenerates and retries the complete notification.
- A crash after an external service accepted a message but before SQLite committed it can produce
  a duplicate on a later run. Delivery is intentionally at least once.
- Accounts run in configuration order. A capture, parse, or total-delivery failure for one account
  does not stop later accounts, but the overall process exits nonzero.

An unchanged non-initial check completes without sending a message.

## Requirements

- Python 3.14 is preferred; Python 3.10 is the supported floor. See the
  [official Python downloads](https://www.python.org/downloads/).
- Google Chrome or Chromium must be installed for native execution.
- When neither `browser.driver_path` nor `CHROMEDRIVER_PATH` is set, Selenium uses
  [Selenium Manager](https://www.selenium.dev/documentation/selenium_manager/) to discover or
  obtain a compatible driver. Its first run can require network access and a writable cache.
  Offline hosts should install a matching driver and configure it explicitly.
- Docker deployment requires Docker Engine or Docker Desktop with
  [Docker Compose](https://docs.docker.com/compose/install/).

## Configuration

`--config` defaults to `./config.toml` relative to the current working directory. Paths inside
that file expand `~`; relative paths are resolved from the configuration file's directory. The
parser supplies no implicit values for required keys. The tracked file contains useful sample
values:

```toml
[storage]
database_path = "data/submissions.db"

[browser]
headless = true
element_timeout_seconds = 30
page_load_timeout_seconds = 60
# binary_path = "/optional/path/to/chrome"
# driver_path = "/optional/path/to/chromedriver"

[[accounts]]
name = "primary"
url = "https://mc.manuscriptcentral.com/example"
username = "${PRIMARY_SCHOLARONE_USERNAME}"
password = "${PRIMARY_SCHOLARONE_PASSWORD}"
manuscript_ids = []
apprise_urls = ["${PRIMARY_APPRISE_URL}"]
```

The accepted keys are:

| Key | Required | Meaning |
| --- | --- | --- |
| `storage.database_path` | yes | SQLite file. Its parent directory is created automatically. |
| `browser.headless` | yes | Boolean selecting headless or visible Chrome. |
| `browser.element_timeout_seconds` | yes | Positive integer wait for login and dashboard elements. |
| `browser.page_load_timeout_seconds` | yes | Positive integer Selenium page-load timeout. |
| `browser.binary_path` | no | Explicit Chrome/Chromium executable path. `CHROME_BIN` is the fallback override. |
| `browser.driver_path` | no | Explicit ChromeDriver path. `CHROMEDRIVER_PATH` is the fallback override. |
| `accounts[].name` | yes | Unique, case-sensitive, durable account identity. Keep it stable. |
| `accounts[].url` | yes | ScholarOne login URL using HTTP or HTTPS. |
| `accounts[].username` | yes | ScholarOne user name. |
| `accounts[].password` | yes | ScholarOne password. Use an environment reference. |
| `accounts[].manuscript_ids` | yes | Empty array tracks all dashboard manuscripts; otherwise only normalized listed short IDs are in scope. |
| `accounts[].apprise_urls` | yes | Nonempty, duplicate-free per-account destination list using [Apprise URL syntax](https://appriseit.com/getting-started/universal-syntax/). |

Repeat `[[accounts]]` for multiple accounts. A nonempty ID filter affects notifications and active
tracking scope, but the complete dashboard must still parse correctly. Removing an ID or account
closes its active tracking period without deleting history or sending a disappearance. Restoring
the same account `name` reuses its identity and sends a fresh `CURRENT` verification; renaming an
account is treated as removal plus a new account.

`${ENV_VAR}` references are recursively expanded in all configuration strings, including list
entries. An unset variable or expansion cycle is a configuration error. Native Python does not
read `.env`; export the referenced variables in the invoking shell or scheduler. Docker Compose
loads the repository `.env` file.

There is deliberately no schedule key in `config.toml`. Native scheduling belongs to the operating
system, and the Docker schedule belongs to `.env` through `CRON_SCHEDULE`.

## Install with pip

POSIX shells:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py --config config.toml
```

Windows PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --config config.toml
```

The final command is one complete check. Export every variable referenced by `config.toml` before
running it.

## Install with uv

Install uv using its [official installation guide](https://docs.astral.sh/uv/getting-started/installation/),
then use the committed lock file:

```bash
uv python install 3.14
uv sync --locked
uv run --locked python main.py --config config.toml
```

`uv sync --locked` refuses to rewrite a stale lock file. The last command remains a one-shot check;
see the official [locking and syncing guide](https://docs.astral.sh/uv/concepts/projects/sync/).

## Native scheduling

First run the one-shot command manually. Scheduler jobs need absolute paths, network access, write
access to the database directory, and the same environment variables as the successful manual
run. Prevent overlapping jobs: one process holds `<database_path>.lock` for its entire invocation,
and a concurrent process exits immediately with code 1.

The examples run every six hours. Replace the example user and installation paths.

### Linux cron

Create `/home/checker/.config/check-submission-status/env` with shell assignments for the variables
referenced by `config.toml`, restrict it with `chmod 600`, then edit the `checker` user's crontab:

```bash
sudo -u checker crontab -e
```

```cron
0 */6 * * * set -a && . /home/checker/.config/check-submission-status/env && set +a && cd /home/checker/check-submission-status && /home/checker/check-submission-status/.venv/bin/python /home/checker/check-submission-status/main.py --config /home/checker/check-submission-status/config.toml >> /home/checker/check-submission-status/data/cron.log 2>&1
```

Cron uses the host's local timezone unless configured otherwise. Consult Cronie's upstream
[`crontab(5)` source](https://github.com/cronie-crond/cronie/blob/master/man/crontab.5).

### Linux systemd timer

Create a root-readable `/etc/check-submission-status.env`, then create
`/etc/systemd/system/check-submission-status.service`:

```ini
[Unit]
Description=Check ScholarOne submission statuses
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=checker
Group=checker
WorkingDirectory=/opt/check-submission-status
EnvironmentFile=/etc/check-submission-status.env
UMask=0077
ExecStart=/opt/check-submission-status/.venv/bin/python /opt/check-submission-status/main.py --config /opt/check-submission-status/config.toml
```

Create `/etc/systemd/system/check-submission-status.timer`:

```ini
[Unit]
Description=Run the ScholarOne status check every six hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
```

Enable and inspect it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now check-submission-status.timer
systemctl list-timers check-submission-status.timer
journalctl -u check-submission-status.service
```

See the upstream [`systemd.timer` manual](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html).

### macOS launchd

Create a protected `/Users/alice/.config/check-submission-status/env` containing shell assignments.
Save the following as
`/Users/alice/Library/LaunchAgents/io.github.check-submission-status.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.github.check-submission-status</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-c</string>
    <string>set -a &amp;&amp; source /Users/alice/.config/check-submission-status/env &amp;&amp; set +a &amp;&amp; exec /Users/alice/check-submission-status/.venv/bin/python /Users/alice/check-submission-status/main.py --config /Users/alice/check-submission-status/config.toml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/alice/check-submission-status</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>21600</integer>
  <key>StandardOutPath</key>
  <string>/Users/alice/Library/Logs/check-submission-status.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/alice/Library/Logs/check-submission-status.error.log</string>
</dict>
</plist>
```

Validate, load, test, and inspect the user agent:

```bash
plutil -lint ~/Library/LaunchAgents/io.github.check-submission-status.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.check-submission-status.plist
launchctl kickstart -k gui/$(id -u)/io.github.check-submission-status
launchctl print gui/$(id -u)/io.github.check-submission-status
```

Use `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/io.github.check-submission-status.plist`
before replacing or removing the job. Apple documents periodic jobs in
[Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

### Windows Task Scheduler

Set the required variables as protected user environment variables for the task account, then sign
out and back in so new background processes inherit them. In PowerShell, register a six-hour task
that directly invokes the one-shot script:

```powershell
$taskCommand = '"C:\Users\Alice\check-submission-status\.venv\Scripts\python.exe" "C:\Users\Alice\check-submission-status\main.py" --config "C:\Users\Alice\check-submission-status\config.toml"'
schtasks.exe /Create /TN "Check Submission Status" /TR $taskCommand /SC HOURLY /MO 6 /F
schtasks.exe /Run /TN "Check Submission Status"
schtasks.exe /Query /TN "Check Submission Status" /V /FO LIST
```

Run the task under the same user whose environment and files you configured. Remove it with
`schtasks.exe /Delete /TN "Check Submission Status" /F`. Microsoft documents all schedule and
account options in [`schtasks /create`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create).

## Docker Compose

### Configure and start

Copy the example environment file, then replace every placeholder and choose the cron schedule and
timezone:

```bash
cp .env.example .env
```

```powershell
Copy-Item .env.example .env
```

```dotenv
CRON_SCHEDULE='0 */6 * * *'
TZ='UTC'
PRIMARY_SCHOLARONE_USERNAME='replace-me'
PRIMARY_SCHOLARONE_PASSWORD='replace-me'
PRIMARY_APPRISE_URL='replace-me'
```

Keep secrets in `.env`; keep `${...}` references in `config.toml`. Validate Compose quietly, then
build and start:

```bash
docker compose config --quiet
docker compose up --build -d
docker compose logs -f checker
```

Always include `--quiet` when validating a secret-bearing deployment. Plain `docker compose config`
renders resolved environment values and can expose secrets in a terminal, log, or support bundle.
`CRON_SCHEDULE` is mandatory; an unset or empty value makes Compose validation fail.

At container startup, Compose validates the generated five-field crontab, performs an immediate
one-shot check, and then starts Supercronic. A transient failure of the immediate check is logged
but scheduled checks continue. Invalid cron syntax prevents the scheduler from starting. `TZ`
controls the cron schedule; application history timestamps remain UTC.

### Logs and lifecycle

```bash
docker compose logs --tail=200 checker
docker compose restart checker
docker compose stop checker
docker compose start checker
docker compose down
```

`restart` performs another immediate check before resuming the schedule. `stop` and `down` preserve
the named data volume. Do not use `docker compose down -v` unless you intend to delete all SQLite
history. See Docker's official [Compose application model](https://docs.docker.com/compose/intro/compose-application-model/).

## SQLite history, backup, and restore

Native runs use `storage.database_path`. Compose mounts the `submission-data` named volume at
`/app/data`, so the sample database is `/app/data/submissions.db`. Docker owns the volume's physical
host location; ordinary container replacement does not remove it.

SQLite retains account identities, configuration revisions, filters, checks, tracking periods,
present and absent observations, accepted current state, complete notification messages, and
redacted per-destination delivery results. Removing an account or filter closes its active history
instead of deleting rows. The database stores ScholarOne URLs and usernames as configuration
history, plus manuscript metadata. It never stores ScholarOne passwords or full Apprise URLs;
delivery rows keep only destination position, scheme, outcome, timestamp, and fixed safe errors.

The database is not encrypted. Protect it and its backups as sensitive manuscript data.

Stop the checker before backing up. The following POSIX-shell flow writes outside the worktree,
opens the source database so SQLite can recover any hot rollback journal, uses the
[SQLite Online Backup API](https://www.sqlite.org/backup.html), and verifies the result:

```bash
(
set -eu
BACKUP_DIR="$HOME/check-submission-status-backups/$(date +%Y%m%d-%H%M%S)"
BACKUP_CONTAINER="check-submission-status-backup-$(date +%s)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
docker compose stop checker
docker compose run --name "$BACKUP_CONTAINER" --no-deps --entrypoint /app/.venv/bin/python checker -c "import sqlite3, sys; expected={'account_configuration_targets','account_configurations','accounts','checks','current_states','manuscripts','notification_batches','notification_deliveries','observations','tracking_periods'}; source=sqlite3.connect('file:/app/data/submissions.db?mode=rw', uri=True); source_tables={row[0] for row in source.execute(\"SELECT name FROM sqlite_master WHERE type = 'table'\")}; sys.exit('source schema check failed') if source.execute('PRAGMA user_version').fetchone()[0] != 1 or not expected <= source_tables else None; target=sqlite3.connect('/tmp/submissions.db'); source.backup(target); target_tables={row[0] for row in target.execute(\"SELECT name FROM sqlite_master WHERE type = 'table'\")}; valid=target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' and target.execute('PRAGMA user_version').fetchone()[0] == 1 and expected <= target_tables; target.close(); source.close(); sys.exit('backup integrity or schema check failed') if not valid else None"
docker cp "$BACKUP_CONTAINER:/tmp/submissions.db" "$BACKUP_DIR/submissions.db"
docker rm "$BACKUP_CONTAINER"
chmod 600 "$BACKUP_DIR/submissions.db"
docker compose start checker
)
```

Keep `BACKUP_DIR` outside the repository and protect it like the live database. If backup creation
fails, remove the retained `BACKUP_CONTAINER` only after inspecting its logs, and restart the
checker when it is safe to do so.

To restore, select a completed backup, stop the checker, verify the backup read-only, remove the
old database and every possible SQLite sidecar, copy into the named volume, correct ownership, and
only then start the checker:

```bash
(
set -eu
BACKUP_DIR="$HOME/check-submission-status-backups/20260710-120000"
docker compose stop checker
docker compose run --rm --no-deps --user root --entrypoint /app/.venv/bin/python -v "$BACKUP_DIR:/backup:ro" checker -c "import sqlite3, sys; expected={'account_configuration_targets','account_configurations','accounts','checks','current_states','manuscripts','notification_batches','notification_deliveries','observations','tracking_periods'}; database=sqlite3.connect('file:/backup/submissions.db?mode=ro', uri=True); tables={row[0] for row in database.execute(\"SELECT name FROM sqlite_master WHERE type = 'table'\")}; valid=database.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' and database.execute('PRAGMA user_version').fetchone()[0] == 1 and expected <= tables; database.close(); sys.exit('backup integrity or schema check failed') if not valid else None"
docker compose run --rm --no-deps --user root --entrypoint /bin/rm checker -f /app/data/submissions.db /app/data/submissions.db-journal /app/data/submissions.db-wal /app/data/submissions.db-shm
docker compose create checker
docker compose cp "$BACKUP_DIR/submissions.db" checker:/app/data/submissions.db
docker compose run --rm --no-deps --user root --entrypoint /bin/chown checker app:app /app/data/submissions.db
docker compose start checker
)
```

The target container may be stopped for `docker compose cp`; see the official
[`docker compose cp` reference](https://docs.docker.com/reference/cli/docker/compose/cp/). For a
native deployment, stop scheduled and manual runs and use Python's `sqlite3.Connection.backup`
instead of copying only the main rollback-journal database file. Verify the backup, preserve its
permissions, and then restart scheduling.

## Troubleshooting and exit codes

- **Login no longer completes:** run once with `browser.headless = false`. The current ScholarOne
  flow expects fields named `USERID` and `PASSWORD`, button `#logInButton`, an `Author` navigation
  link, and table `#authorDashboardQueue`. Site-specific markup changes require selector updates.
- **Dashboard table is missing or malformed:** the entire account check fails safely; it is never
  treated as an empty dashboard and accepted state does not advance. Confirm the URL, credentials,
  author role, and current ScholarOne layout.
- **Chrome or driver cannot start:** ensure Chrome and ChromeDriver major versions match. Configure
  `browser.binary_path` and `browser.driver_path`, or set `CHROME_BIN` and `CHROMEDRIVER_PATH`.
  Remove the overrides to return to Selenium Manager.
- **Notifications repeat:** if all destinations failed, repetition is expected because state was not
  committed. Test each Apprise URL. If at least one destination succeeded, the change was committed
  and failed sibling destinations are not retried.
- **Lock conflict:** another process is using the same database. Wait for it to exit, inspect the
  scheduler for overlap, and do not delete the `.lock` file as a substitute for stopping a live
  process.
- **Configuration exits before opening Chrome:** all validation errors are reported together. Check
  unknown or missing keys, path resolution, HTTP(S) account URLs, positive timeouts, duplicate
  names/destinations, and exported environment variables.

Process exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Every configured account succeeded, including an empty account list. `--help` also exits 0. |
| `1` | At least one account, notification, browser, parser, database, or process-lock operation failed. |
| `2` | Command-line usage or configuration loading/validation failed. |

Docker's Supercronic process remains alive after an individual scheduled check exits 1; inspect the
service logs for the per-run result.

## Security

- Never commit `.env`, a populated secret-bearing configuration, SQLite data, backups, browser
  profiles, or logs. The repository ignores `.env`, `/data`, local tests, and demo fixtures.
- Prefer `${ENV_VAR}` references over literal passwords and Apprise URLs. Restrict environment
  files to the scheduler account (`chmod 600` on POSIX) and use equivalent Windows ACLs.
- Treat Apprise URLs as credentials. Do not paste them into issue reports or run unquiet Compose
  rendering in captured terminals.
- Restrict the SQLite file even though passwords and full destinations are omitted: it contains
  usernames, ScholarOne URLs, manuscript titles, statuses, and complete notification bodies.
- Run native schedulers with an unprivileged dedicated account. The container already runs as the
  non-root `app` user.

For the complete Chinese counterpart, see [README_zh.md](README_zh.md).

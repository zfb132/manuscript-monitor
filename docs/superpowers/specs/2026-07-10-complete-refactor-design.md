# Complete Project Refactor Design

## Summary

Refactor the project into a cross-platform, single-file Python application that checks ScholarOne manuscript dashboards, stores durable history in SQLite, and sends notifications through Apprise.

The only production Python source file will be `main.py`. Local execution always performs one check and exits. Docker Compose performs an immediate check when the container starts, then invokes the same one-shot command on a crontab schedule supplied through `.env`.

## Goals

- Keep all production Python code in one `main.py` file with no imports from local Python modules.
- Convert every tracked file to English except `README_zh.md`, which is the only file allowed to contain Chinese text.
- Support native execution on Linux, macOS, and Windows.
- Support Docker Compose with an immediate check followed by crontab-compatible scheduling.
- Provide unversioned `requirements.txt` and uv project metadata, plus a reproducible `uv.lock`.
- Prefer Python 3.14 while supporting the lowest Python version accepted by the latest direct dependencies.
- Replace direct SMTP handling with per-account Apprise destinations.
- Replace Python module configuration with `config.toml`.
- Support multiple ScholarOne accounts.
- Track all manuscripts by default, or a configured set of short manuscript IDs.
- Parse and store manuscript ID, title, submission date, and status.
- Store account configuration history, every account check, manuscript observations, tracking changes, and notification results in one SQLite database.
- Preserve historical rows when accounts or manuscript filters change.
- Notify on the initial successful check, newly tracked manuscripts, status changes, disappearance, and reappearance.
- Combine all events for one account and one check into one complete notification message.
- Commit a state transition when any configured notification destination succeeds.
- Retain the previous committed state when all notification destinations fail so the next run retries the change.

## Non-Goals

- The application will not run its own scheduling loop.
- `config.toml` will not contain scheduling fields.
- Title-only or submission-date-only changes will be stored but will not trigger notifications.
- Failed Apprise destinations will not be retried after another destination succeeds.
- ScholarOne passwords and complete Apprise URLs will never be stored in SQLite or logs.
- Removing an account or manuscript ID from `config.toml` will not send a notification.
- Unit-test files and HTML fixtures will not be tracked.

## Repository Layout

The tracked project will contain these primary files:

```text
.env.example
.github/workflows/ci.yml
.python-version
.dockerignore
.gitignore
Dockerfile
LICENSE
README.md
README_zh.md
config.toml
docker-compose.yml
docs/superpowers/specs/2026-07-10-complete-refactor-design.md
main.py
pyproject.toml
requirements.txt
uv.lock
```

The legacy `config.example.py`, `configure.sh`, and `log.py` files will be removed. Ignored local verification files will include `demo.html` and the entire `tests/` directory.

## Runtime and Dependency Policy

Python 3.14 is the preferred development and Docker runtime and will be recorded in `.python-version`. The project floor will be Python 3.10 because the current latest Selenium and filelock releases require Python 3.10 or newer. Python 3.10 uses `tomli`; Python 3.11 and newer use `tomllib` from the standard library.

Runtime declarations contain names and environment markers only, with no package version constraints:

```text
apprise
beautifulsoup4
filelock
selenium
tomli; python_version < "3.11"
```

The same list appears in `requirements.txt` and `pyproject.toml`. The uv development group adds unversioned `pytest` and `ruff`. The generated `uv.lock` contains exact resolved versions because reproducibility is the lockfile's purpose. `[tool.uv] package = false` keeps this a script application rather than an installable local package.

## Single-File Architecture

`main.py` is organized into focused sections without becoming a package:

1. Constants, exceptions, and immutable data classes.
2. CLI parsing, TOML loading, environment expansion, and validation.
3. Logging and secret-safe error formatting.
4. SQLite connections, migrations, repositories, and transactions.
5. Selenium driver construction, login, navigation, and page capture.
6. BeautifulSoup manuscript parsing and normalization.
7. Tracking-scope reconciliation and difference calculation.
8. Notification rendering and per-destination Apprise delivery.
9. Per-account orchestration and exit-code aggregation.

Importing `main.py` creates no directories, database connections, log handlers, or browsers. Side effects begin only from the CLI entry point.

Accounts are checked sequentially with a fresh browser session. A failure for one account is recorded and does not prevent later accounts from running. The process exits nonzero if any account fails.

## Configuration

The tracked `config.toml` contains safe placeholders and this structure:

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
name = "primary-tap"
url = "https://mc.manuscriptcentral.com/tap-ieee"
username = "${PRIMARY_SCHOLARONE_USERNAME}"
password = "${PRIMARY_SCHOLARONE_PASSWORD}"
manuscript_ids = []
apprise_urls = ["${PRIMARY_APPRISE_URL}"]

[[accounts]]
name = "secondary-journal"
url = "https://mc.manuscriptcentral.com/example"
username = "${SECONDARY_SCHOLARONE_USERNAME}"
password = "${SECONDARY_SCHOLARONE_PASSWORD}"
manuscript_ids = ["AP2606-1126", "AP2606-1287"]
apprise_urls = [
  "tgram://bot-token/chat-id",
  "mailto://user:password@example.com",
]
```

Configuration rules:

- Account `name` is required, unique, and stable across configuration changes.
- An empty `manuscript_ids` list tracks every manuscript found for that account.
- Manuscript filters are trimmed, deduplicated, and normalized by discarding the first opening parenthesis and everything after it.
- Every account requires at least one Apprise URL.
- `${ENV_VAR}` references expand recursively in strings and string lists. Literal values remain valid.
- A referenced but unset environment variable is a configuration error.
- Account names, URLs, usernames, filter IDs, and destinations reject empty values after trimming. Password contents are not modified.
- Browser timeouts must be positive integers.
- Relative database paths resolve from the directory containing `config.toml`, not from the current working directory.
- The file contains no schedule, interval, or cron setting.
- CLI and configuration errors are reported without credentials.

Native commands are:

```bash
python main.py --config config.toml
uv run main.py --config config.toml
```

Both perform exactly one check and exit.

## SQLite Model

The database uses foreign keys, explicit transactions, UTC ISO 8601 timestamps, and migrations controlled by `PRAGMA user_version`. It retains SQLite's default rollback-journal mode rather than enabling persistent WAL sidecar files.

### `accounts`

- `id` integer primary key.
- `name` unique stable configuration name.
- `active` current configuration-presence flag.
- `needs_initial_notification` current activation's verification flag.
- `created_at` and `updated_at` timestamps.

### `account_configurations`

- `id` integer primary key.
- `account_id` foreign key.
- `url` and `username` for this non-secret revision.
- `track_all` boolean.
- `active_from` and nullable `active_until` timestamps.

A row is added when URL, username, or the manuscript filter changes, or when an inactive account is reintroduced. Password and Apprise changes are intentionally absent because those values are not stored.

### `account_configuration_targets`

- `configuration_id` foreign key.
- `external_id` normalized short manuscript ID.
- Composite primary key on both columns.

### `checks`

- `id` integer primary key.
- `configuration_id` foreign key.
- `started_at` and nullable `completed_at` timestamps.
- `outcome` constrained to `running`, `succeeded`, or `failed`.
- `parsed_count` nullable integer.
- `error_type` and `error_message` nullable redacted text.

### `manuscripts`

- `id` integer primary key.
- `account_id` foreign key.
- `external_id` normalized short manuscript ID.
- `created_at` database-row timestamp.
- `first_seen_at` nullable first-page-observation timestamp.
- Unique constraint on `(account_id, external_id)`.

An explicitly configured ID may have a row before it first appears on the page.

### `tracking_periods`

- `id` integer primary key.
- `manuscript_id` foreign key.
- `started_at` and nullable `ended_at` timestamps.
- `started_reason` constrained to account activation, filter addition, track-all discovery, or scope reactivation.
- `ended_reason` nullable and constrained to account removal or filter removal.

Removing and later restoring a target closes one period and creates another rather than deleting or reusing history.

### `observations`

- `id` integer primary key.
- `check_id`, `manuscript_id`, and `tracking_period_id` foreign keys.
- `present` boolean.
- `title`, `submitted_text`, `submitted_date`, and `status`, required when present and nullable when absent.
- `observed_at` timestamp.
- Unique constraint on `(check_id, tracking_period_id)`.

### `current_states`

- `tracking_period_id` primary key and foreign key.
- `observation_id` foreign key to the most recent accepted observation.
- `updated_at` timestamp.

Title-only and submission-date-only changes update this projection after a successful scan because they do not require notification. A notification-bearing transition updates it only after at least one delivery succeeds.

### `notification_batches`

- `id` integer primary key.
- `check_id` and `account_id` foreign keys.
- `reason` constrained to initial verification or manuscript changes.
- `title` and complete `body` text.
- `event_count` integer.
- `created_at` and nullable `committed_at` timestamps.

An initial successful scan with no manuscripts still creates a verification batch with zero manuscript events.

### `notification_deliveries`

- `id` integer primary key.
- `batch_id` foreign key.
- `destination_index` zero-based account-list position.
- `scheme` safe Apprise protocol identifier.
- `attempted_at` timestamp.
- `success` boolean.
- `error_type` and `error_message` nullable redacted text.

No migration or normal operation deletes business-history rows. Current flags, validity end times, and projection pointers may be updated.

## Configuration Reconciliation

The application reconciles `config.toml` before scraping:

- A new account creates an active account and revision and requires an initial notification.
- An omitted account is marked inactive, its revision is closed, and active tracking periods end with `account removal`. This generates no disappearance event.
- Reintroducing the same name creates a revision and new periods and requires a complete current-state notification.
- URL or username changes create a revision but preserve active periods and committed states.
- Moving from all manuscripts to explicit filters closes non-target periods without notifications.
- Adding explicit filters opens periods. A target absent from the page stays uninitialized and is not disappeared; its first later appearance is `NEW`.
- Moving from explicit filters to all manuscripts opens periods only for manuscripts present in the next complete page. Previously stored but currently absent out-of-scope manuscripts are not disappeared.
- Password-only and Apprise-only changes require no database reconciliation.

## Browser Automation

The Selenium flow is:

1. Create a new Chrome or Chromium session for one account.
2. Open the configured ScholarOne URL.
3. Fill fields named `USERID` and `PASSWORD`.
4. Wait for and click `logInButton`.
5. Wait for the author navigation link, normalize its text, and enter `Author`.
6. Wait for `table#authorDashboardQueue`.
7. Capture `driver.page_source`.
8. Always call `driver.quit()` in `finally`.

Driver and browser path precedence is:

1. Explicit `browser.driver_path` and `browser.binary_path`.
2. Docker `CHROMEDRIVER_PATH` and `CHROME_BIN` environment variables.
3. Selenium Manager discovery and management.

Headless mode defaults to enabled. Browser construction uses platform-neutral paths and no hard-coded user agent. Linux-only Chrome flags are applied only for Docker when needed.

## HTML Parsing

BeautifulSoup uses Python's built-in `html.parser`, so `lxml` is not required. Parsing is row-scoped:

1. Find `table#authorDashboardQueue`.
2. Require its `tbody`.
3. Iterate direct manuscript `tr` children.
4. Within each row, locate the relevant `td[data-label]` cells.

Field rules:

- Status is `td[data-label="status"] span.pagecontents`.
- ID is `td[data-label="ID"]`, with whitespace collapsed and the first `(` plus everything after it removed.
- Title is a copy of `td[data-label="title"]` with anchor elements removed, excluding `View Submission`.
- Submission text is `td[data-label="submitted"]`; the display text is retained and English `DD-Mon-YYYY` is normalized to ISO `YYYY-MM-DD` without host-locale dependence.
- Every value is stripped and internal whitespace is collapsed.

A present row with a missing or empty required field, invalid date, or duplicate ID invalidates the entire account parse. A present table with a valid empty `tbody` is a successful zero-manuscript result. A missing table is an error, not an empty result. Filtering occurs only after the complete table parses successfully.

## Difference Semantics

Successful normalized results are compared with `current_states` only within active tracking periods:

- `CURRENT`: every present manuscript while an activation still requires initial verification.
- `NEW`: a present manuscript with a new period and no accepted present state.
- `STATUS_CHANGED`: a present manuscript whose status differs from its accepted present state.
- `DISAPPEARED`: a previously accepted present manuscript still in scope but absent from a complete page.
- `REAPPEARED`: a manuscript whose accepted state is absent and is present again.

Title-only and date-only changes add observations and refresh accepted display data without a notification. An unchanged successful check adds observations and updates safe projections without a notification batch.

## Notification Semantics

Each account owns its own `apprise_urls`. Different accounts are never combined. All events for one account and one check are combined into one plain-text notification, and every manuscript entry remains complete.

Each entry includes event type, short ID, title, displayed submission date, and current status. Status changes include previous and current status. Disappearances include the last known status.

The first successful check sends all current manuscripts as `CURRENT`. If none exist, it sends a channel-verification message stating that no manuscripts were found in the current scope.

Each destination is loaded and notified separately. The batch commits when any destination succeeds. Failed destinations in a partially successful batch are not retried. If all fail, the batch remains uncommitted, notification-bearing states do not advance, the account check fails, and the next invocation regenerates the change.

Complete messages are not truncated for provider-specific limits. One provider may fail while another succeeds.

## Transactions and Failures

The process acquires a cross-platform file lock adjacent to the database before initialization and holds it for the invocation. A conflict fails instead of overlapping another run.

For each account:

1. Insert a running check.
2. Scrape and parse outside a long SQLite write transaction.
3. Insert manuscripts, tracking changes, and observations and calculate the proposed batch in a short transaction.
4. Send each destination outside the database transaction.
5. Record each delivery.
6. If any succeeds, atomically commit the batch, clear the initial flag when applicable, and advance affected current states.
7. If all fail, leave notification-bearing projections unchanged.
8. Mark the check succeeded or failed with a redacted result.

A crash after an external service accepts a message but before SQLite commits may duplicate the notification on the next run. This at-least-once behavior is preferred to silently losing a transition; exact-once delivery is impossible without transactional support from every service.

An account error does not stop later accounts. Errors never become empty dashboards or disappearance events.

Exit codes are:

- `0`: all account checks succeeded and each required batch had a successful destination.
- `1`: a runtime, account, database, lock, or notification operation failed.
- `2`: CLI or configuration validation failed before normal processing.

Logs go to standard output and error. They identify accounts by name and destinations by index and protocol only, never by credentials or complete URLs.

## Docker Compose

The image uses Python 3.14 slim and installs Chromium, ChromeDriver, CA certificates, and timezone data. It runs as a non-root application user. A checksum-verified Supercronic release provides container-specific crontab execution; this external binary pin is separate from the unversioned PyPI policy.

Compose will:

- Mount `config.toml` read-only at `/app/config.toml`.
- Persist `/app/data` in a named volume.
- Load `.env` so credentials, Apprise URLs, `CRON_SCHEDULE`, and `TZ` reach immediate and scheduled jobs.
- Set Docker-only browser and driver paths through environment variables.
- Validate the generated crontab.
- Run `python main.py --config /app/config.toml` immediately.
- Start Supercronic with the same command on `CRON_SCHEDULE`.
- Continue scheduling after a transient immediate failure.
- Use `restart: unless-stopped` and Compose `init: true`.

The example schedule is every six hours and the default timezone is UTC. Both live only in `.env`. Supercronic prevents the same scheduled job from overlapping, while the file lock also blocks manual overlap.

## Native Platform Support

- Paths use `pathlib` and explicit UTF-8 handling.
- SQLite and TOML use standard cross-platform APIs.
- Selenium Manager is the default native driver mechanism.
- Application code has no Bash, `/etc`, `/usr/bin/python3`, systemd, or POSIX-only assumptions.
- Both READMEs document one-shot execution and user-owned scheduling through cron or systemd timers on Linux, launchd on macOS, and Task Scheduler on Windows.

## Documentation and Language Policy

`README.md` is the canonical English guide. `README_zh.md` is a complete Chinese counterpart and the only tracked file permitted to contain Han characters. Both cover purpose, fields, Python support, pip, uv, configuration, secrets, all three native platforms, Docker scheduling, persistence, notification rules, security, and troubleshooting.

All code, comments, docstrings, configuration comments, Docker metadata, CI, design documents, and runtime messages remain English. The rule applies to the current tracked tree, not historical Git objects.

## Verification Strategy

Local tests and fixtures are ignored and never staged:

```text
demo.html
tests/test_config.py
tests/test_database.py
tests/test_parser.py
tests/test_diff.py
tests/test_notifications.py
tests/test_workflow.py
```

`demo.html` contains the approved ScholarOne table structure. Ignored tests cover configuration, parsing, malformed pages, database migrations, every transition, configuration reconciliation, notification success policies, message completeness, secret redaction, browser cleanup, account isolation, exit codes, and file locking.

Final local verification commands are:

```bash
uv lock
uv sync
uv run pytest
uv run ruff check main.py tests
uv run ruff format --check main.py tests
uv run python -m py_compile main.py
docker compose config
docker build .
```

A tracked GitHub Actions smoke matrix contains no unit-test cases or HTML fixtures. On Linux, macOS, and Windows with Python 3.10 and 3.14, it installs the locked environment, compiles and imports `main.py`, exercises CLI help, and runs Ruff on tracked Python. A Linux job validates Compose and builds the image. Another check fails if a tracked file other than `README_zh.md` contains Han characters.

## Acceptance Criteria

- `git ls-files '*.py'` lists `main.py` as the only tracked Python source.
- No tracked file except `README_zh.md` contains Chinese text.
- Native Python and uv commands perform one check and exit.
- Latest unversioned dependencies resolve for Python 3.14 and the Python 3.10 floor.
- Parsing extracts and normalizes every approved HTML field.
- Multiple accounts and all/filtered modes operate independently.
- SQLite preserves configurations, checks, observations, periods, and deliveries without credentials.
- Configuration removals generate neither deletions nor disappearance notifications.
- Initial, new, status-change, disappearance, and reappearance notifications follow the approved rules.
- Any successful destination commits a batch; an entirely failed batch retries next time.
- Compose checks immediately and then invokes the same command on `CRON_SCHEDULE`.
- Docker data survives container recreation.
- Ignored local tests pass, tracked smoke checks pass, and the image builds.

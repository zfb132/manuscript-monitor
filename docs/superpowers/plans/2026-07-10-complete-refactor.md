# Complete Project Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current Linux-only SMTP script with a one-shot, single-file, cross-platform ScholarOne monitor that stores complete SQLite history, supports multiple accounts and manuscript filters, sends per-account Apprise notifications, and runs natively or on a Docker Compose cron schedule.

**Architecture:** All production Python behavior lives in `main.py`, but the file is organized around immutable data types and explicit functional boundaries for configuration, parsing, persistence, differences, notifications, Selenium, and orchestration. SQLite stores append-preserving history plus a current accepted-state projection; browser and Apprise calls occur outside write transactions. Native runs are one-shot, while Compose performs one immediate run and schedules the same command with Supercronic.

**Tech Stack:** Python 3.14 preferred with Python 3.10 floor, stdlib `argparse`/`datetime`/`pathlib`/`sqlite3`/`tomllib`, conditional `tomli`, Selenium, Beautiful Soup, Apprise, filelock, pytest, Ruff, uv, Chromium, Docker Compose, Supercronic, GitHub Actions.

## Global Constraints

- `main.py` is the only tracked Python source file and imports no local Python module.
- Production code, comments, configuration, metadata, CI, and documentation are English; `README_zh.md` is the only tracked file allowed to contain Chinese.
- `.python-version` and Docker use Python 3.14; `requires-python` is `>=3.10`.
- Direct dependency declarations contain no version specifiers. Exact versions belong only in `uv.lock` and pinned infrastructure references.
- `python main.py --config config.toml` and `uv run main.py --config config.toml` perform one check and exit.
- `config.toml` contains no scheduling value. Docker scheduling comes only from `.env` through `CRON_SCHEDULE`.
- Multiple accounts are processed sequentially and independently. Empty `accounts` is valid so a run can deactivate every previously configured account without deleting history.
- Unknown TOML keys are rejected. Duplicate manuscript IDs are stably deduplicated; duplicate Apprise URLs are rejected without printing their values.
- Passwords and user-supplied, real, or credential-bearing Apprise destinations never enter SQLite,
  logs, exception text, committed test data, or tracked planning examples. Safe `.invalid` and
  localhost destinations may appear in ignored tests and documentation examples.
- The existing `demo.html` and every file under `tests/` remain ignored and are never staged or committed.
- `parsed_count` means all valid dashboard manuscript rows before account filtering.
- Configuration removal ends tracking scope without a disappearance event. Business-history rows are never deleted.
- Initial populated and empty checks notify. New, status-changed, disappeared, and reappeared manuscripts notify. Title-only and date-only changes do not.
- One account/check produces one complete message. Any destination success commits notification-bearing state; total failure retries it next run.
- Safe non-event projections advance even when another manuscript's notification batch fails.
- All production commits run the complete ignored local suite first and stage only explicit tracked paths.

## File Structure

**Tracked files created or replaced:**

- `main.py`: every production type and behavior.
- `config.toml`: safe, English TOML configuration with environment references.
- `requirements.txt`: unversioned runtime dependency names.
- `pyproject.toml`: application metadata, unversioned runtime/dev dependencies, uv, pytest, and Ruff settings.
- `uv.lock`: universal resolved lockfile.
- `.python-version`: preferred Python `3.14`.
- `.env.example`: Compose schedule, timezone, and example secret variables.
- `Dockerfile`: Python 3.14/uv/Chromium/Supercronic image.
- `docker-compose.yml`: immediate run, cron scheduling, secrets, and persistent named volume.
- `.dockerignore`: minimal Docker build context.
- `.github/workflows/ci.yml`: test-code-free native and Docker smoke matrix plus repository policy checks.
- `README.md`: complete English guide.
- `README_zh.md`: complete Chinese counterpart.
- `.gitignore`: runtime secrets/data plus local test and fixture exclusions.

**Tracked files removed:**

- `config.example.py`: superseded by `config.toml`.
- `log.py`: logging is folded into `main.py`.
- `configure.sh`: Linux-only installer is superseded by portable native instructions and Compose.

**Ignored local files used for TDD:**

- `demo.html`: user-provided browser-saved view-source fixture.
- `tests/conftest.py`: fixture unwrapping and shared builders.
- `tests/test_config.py`: configuration and normalization.
- `tests/test_parser.py`: ScholarOne HTML parsing.
- `tests/test_database.py`: schema, recovery, and configuration reconciliation.
- `tests/test_diff.py`: accepted-state transitions and projection semantics.
- `tests/test_notifications.py`: complete message rendering and Apprise delivery.
- `tests/test_browser.py`: Selenium construction, waits, and cleanup.
- `tests/test_workflow.py`: multi-account orchestration, retry, locking, and exit codes.
- `tests/test_repository.py`: dependency, language, tracked-file, and container policy.

## Shared Interfaces

Every task must preserve these names and types so later tasks can consume earlier work:

```python
@dataclass(frozen=True, slots=True)
class StorageConfig:
    database_path: Path


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    headless: bool
    element_timeout_seconds: int
    page_load_timeout_seconds: int
    binary_path: Path | None
    driver_path: Path | None


@dataclass(frozen=True, slots=True)
class AccountConfig:
    name: str
    url: str
    username: str
    password: str = field(repr=False)
    manuscript_ids: tuple[str, ...]
    apprise_urls: tuple[str, ...] = field(repr=False)

    @property
    def track_all(self) -> bool:
        return not self.manuscript_ids


@dataclass(frozen=True, slots=True)
class AppConfig:
    storage: StorageConfig
    browser: BrowserConfig
    accounts: tuple[AccountConfig, ...]


@dataclass(frozen=True, slots=True)
class ManuscriptSnapshot:
    external_id: str
    title: str
    submitted_text: str
    submitted_date: date
    status: str


class EventType(str, Enum):
    CURRENT = "CURRENT"
    NEW = "NEW"
    STATUS_CHANGED = "STATUS_CHANGED"
    DISAPPEARED = "DISAPPEARED"
    REAPPEARED = "REAPPEARED"


@dataclass(frozen=True, slots=True)
class AcceptedState:
    tracking_period_id: int
    observation_id: int
    present: bool
    snapshot: ManuscriptSnapshot | None


@dataclass(frozen=True, slots=True)
class ManuscriptEvent:
    kind: EventType
    current: ManuscriptSnapshot | None
    previous: ManuscriptSnapshot | None

    @property
    def external_id(self) -> str:
        snapshot = self.current or self.previous
        if snapshot is None:
            raise RuntimeError("A manuscript event requires a snapshot.")
        return snapshot.external_id


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    reason: str
    title: str
    body: str
    event_count: int


@dataclass(frozen=True, slots=True)
class RedactedError:
    error_type: str
    error_message: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    destination_index: int
    scheme: str
    attempted_at: datetime
    success: bool
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciledAccount:
    account_id: int
    configuration_id: int
    config: AccountConfig
    target_ids: frozenset[str]
    needs_initial_notification: bool


@dataclass(frozen=True, slots=True)
class ProjectionUpdate:
    tracking_period_id: int
    observation_id: int


@dataclass(frozen=True, slots=True)
class StoredNotificationBatch:
    id: int
    title: str
    body: str
    event_count: int
    reason: str


@dataclass(frozen=True, slots=True)
class PreparedCheck:
    check_id: int
    account_id: int
    configuration_id: int
    parsed_count: int
    events: tuple[ManuscriptEvent, ...]
    batch: StoredNotificationBatch | None
    deferred_updates: tuple[ProjectionUpdate, ...]
    clear_initial_on_commit: bool
```

---

### Task 1: Dependency Baseline and Validated TOML Configuration

**Files:**
- Create: `.python-version`
- Create: `requirements.txt`
- Create: `pyproject.toml`
- Create: `uv.lock` with `uv lock --upgrade`
- Create: `config.toml`
- Modify: `.gitignore`
- Replace: `main.py`
- Delete: `config.example.py`
- Delete: `log.py`
- Test locally only: `tests/test_config.py`

**Interfaces:**
- Produces: `ConfigError`, `StorageConfig`, `BrowserConfig`, `AccountConfig`, `AppConfig`.
- Produces: `normalize_whitespace(value: str) -> str`.
- Produces: `normalize_manuscript_id(value: str) -> str`.
- Produces: `expand_environment(value: str, environ: Mapping[str, str]) -> str`.
- Produces: `parse_config(raw: Mapping[str, object], *, config_dir: Path, environ: Mapping[str, str]) -> AppConfig`.
- Produces: `load_config(path: Path, *, environ: Mapping[str, str] | None = None) -> AppConfig`.

- [ ] **Step 1: Ignore the local fixture, tests, secrets, and runtime data before creating test files**

Add root-anchored entries while retaining useful Python tooling ignores:

```gitignore
/.env
/.venv/
/data/
/demo.html
/tests/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
```

Verify without staging `demo.html`:

```bash
mkdir -p tests
touch tests/test_config.py
git check-ignore -v demo.html tests/test_config.py
```

Expected: both paths resolve to the new root `.gitignore` rules. Do not run `git add -A` anywhere in this project.

- [ ] **Step 2: Add unversioned Python and uv metadata**

Create `.python-version`:

```text
3.14
```

Create `requirements.txt`:

```text
apprise
beautifulsoup4
filelock
selenium
tomli; python_version < "3.11"
```

Create `pyproject.toml`:

```toml
[project]
name = "check-submission-status"
version = "0.1.0"
description = "Monitor ScholarOne manuscript statuses and send Apprise notifications."
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
  "apprise",
  "beautifulsoup4",
  "filelock",
  "selenium",
  "tomli; python_version < '3.11'",
]

[dependency-groups]
dev = [
  "pytest",
  "ruff",
]

[tool.uv]
package = false

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["B", "E", "F", "I", "SIM", "UP"]
```

Run:

```bash
uv lock --upgrade
uv sync --locked
```

Expected: both commands succeed on the preferred Python 3.14 runtime and `uv.lock` is created.

- [ ] **Step 3: Write the first failing configuration tests**

Create ignored `tests/test_config.py` with focused behavior:

```python
from pathlib import Path

import pytest

import main


def test_load_config_expands_secrets_and_normalizes_ids(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[storage]
database_path = "state/submissions.db"

[browser]
headless = true
element_timeout_seconds = 30
page_load_timeout_seconds = 60

[[accounts]]
name = "primary"
url = "https://mc.manuscriptcentral.com/example"
username = "${USER_NAME}"
password = "${USER_PASSWORD}"
manuscript_ids = [" AP2606-0001 (LONG) ", "AP2606-0001"]
apprise_urls = ["${APPRISE_URL}"]
""".strip(),
        encoding="utf-8",
    )

    config = main.load_config(
        path,
        environ={
            "USER_NAME": "author@example.com",
            "USER_PASSWORD": "  keep password whitespace  ",
            "APPRISE_URL": "json://localhost",
        },
    )

    assert config.storage.database_path == tmp_path / "state/submissions.db"
    assert config.accounts[0].manuscript_ids == ("AP2606-0001",)
    assert config.accounts[0].password == "  keep password whitespace  "
    assert "keep password" not in repr(config)
    assert "json://localhost" not in repr(config)


def test_load_config_reports_unset_environment_variable_without_secrets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[storage]
database_path = "submissions.db"
[browser]
headless = true
element_timeout_seconds = 30
page_load_timeout_seconds = 60
[[accounts]]
name = "primary"
url = "https://mc.manuscriptcentral.com/example"
username = "${MISSING_USERNAME}"
password = "secret-value"
manuscript_ids = []
apprise_urls = ["json://localhost"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(main.ConfigError, match="MISSING_USERNAME") as error:
        main.load_config(path, environ={})

    assert "secret-value" not in str(error.value)
```

- [ ] **Step 4: Run the configuration tests and verify RED**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: FAIL because the legacy `main.py` imports the missing local `config` module or because `load_config` is absent.

- [ ] **Step 5: Replace the legacy entry module with the configuration foundation**

Start `main.py` with the complete production import block so every later snippet has an explicit
owner:

```python
import argparse
import logging
import os
import re
import sqlite3
from collections.abc import AbstractSet, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import apprise
from bs4 import BeautifulSoup, Tag
from filelock import FileLock, Timeout as FileLockTimeout
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


LOGGER = logging.getLogger(__name__)
```

Implement the shared configuration data classes and these exact normalization primitives:

```python
ENVIRONMENT_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Configuration is invalid:\n- " + "\n- ".join(self.errors))


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def normalize_manuscript_id(value: str) -> str:
    return normalize_whitespace(value.split("(", 1)[0])


def expand_environment(value: str, environ: Mapping[str, str]) -> str:
    current = value
    seen: set[str] = set()
    while True:
        references = tuple(ENVIRONMENT_REFERENCE.finditer(current))
        if not references:
            return current
        if current in seen:
            raise ConfigError(("Environment expansion contains a cycle.",))
        seen.add(current)

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in environ:
                raise ConfigError((f"Environment variable {name} is not set.",))
            return environ[name]

        current = ENVIRONMENT_REFERENCE.sub(replace, current)
```

Implement `parse_config` with explicit allowed-key sets:

```python
ROOT_KEYS = frozenset({"storage", "browser", "accounts"})
STORAGE_KEYS = frozenset({"database_path"})
BROWSER_KEYS = frozenset(
    {
        "headless",
        "element_timeout_seconds",
        "page_load_timeout_seconds",
        "binary_path",
        "driver_path",
    }
)
ACCOUNT_KEYS = frozenset(
    {"name", "url", "username", "password", "manuscript_ids", "apprise_urls"}
)
```

Validation must collect path-specific messages, reject booleans where a positive integer is required, require HTTP(S) account URLs, permit zero accounts, preserve password contents, resolve relative database/browser/driver paths from `config_dir`, and never interpolate values into error messages. Use `dict.fromkeys()` for stable manuscript-ID deduplication. Reject duplicate account names and duplicate destination positions without printing the secret URLs.

Use the `tomllib`/`tomli` alias established in the production import block:

```python
def load_config(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    config_path = path.expanduser().resolve()
    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError((f"Unable to load configuration: {type(exc).__name__}.",)) from exc
    return parse_config(
        raw,
        config_dir=config_path.parent,
        environ=os.environ if environ is None else environ,
    )
```

Create tracked `config.toml` with one safe environment-backed account:

```toml
[storage]
# Relative paths are resolved from this file's directory.
database_path = "data/submissions.db"

[browser]
headless = true
element_timeout_seconds = 30
page_load_timeout_seconds = 60
# binary_path = "/optional/path/to/chrome"
# driver_path = "/optional/path/to/chromedriver"

[[accounts]]
name = "primary"
url = "https://mc.manuscriptcentral.com/tap-ieee"
username = "${PRIMARY_SCHOLARONE_USERNAME}"
password = "${PRIMARY_SCHOLARONE_PASSWORD}"
# An empty list tracks every manuscript. Add short IDs to filter the dashboard.
manuscript_ids = []
apprise_urls = ["${PRIMARY_APPRISE_URL}"]
```

Remove direct SMTP imports, wildcard configuration imports, and import-time side effects from `main.py`. Delete `config.example.py` and `log.py`.

- [ ] **Step 6: Add validation edge cases and verify GREEN**

Extend ignored tests for unknown keys, recursive expansion cycles, duplicate names, duplicate destinations, zero accounts, malformed account URLs, empty normalized IDs, optional paths, and `True` supplied as a timeout.

Run:

```bash
uv run pytest tests/test_config.py -v
uv run ruff check main.py tests/test_config.py
uv run ruff format --check main.py tests/test_config.py
```

Expected: all configuration tests pass and Ruff reports no issues.

- [ ] **Step 7: Commit the configuration foundation without local tests**

```bash
git add .gitignore .python-version config.toml main.py pyproject.toml \
  requirements.txt uv.lock config.example.py log.py
git diff --cached --check
git commit -m "refactor: add single-file configuration foundation"
```

Expected: the ignored `tests/` directory and `demo.html` do not appear in the staged diff.

---

### Task 2: Robust ScholarOne Dashboard Parser

**Files:**
- Modify: `main.py`
- Test locally only: `tests/conftest.py`
- Test locally only: `tests/test_parser.py`
- Read locally only: `demo.html`

**Interfaces:**
- Consumes: `normalize_whitespace`, `normalize_manuscript_id`.
- Produces: `DashboardParseError`, `ManuscriptSnapshot`.
- Produces: `parse_submitted_date(value: str) -> date`.
- Produces: `parse_dashboard(html: str) -> tuple[ManuscriptSnapshot, ...]`.

- [ ] **Step 1: Write the saved-view-source test helper and parser RED tests**

Create ignored `tests/conftest.py`:

```python
from pathlib import Path

from bs4 import BeautifulSoup


def unwrap_saved_view_source(path: Path) -> str:
    wrapper = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    cells = wrapper.select("td.line-content")
    if not cells:
        raise AssertionError("The saved-source fixture has no line-content cells.")
    return "\n".join(cell.get_text("", strip=False) for cell in cells)
```

Create ignored `tests/test_parser.py` with an exact synthetic row that contains no private fixture data:

```python
from pathlib import Path

import pytest

import main
from conftest import unwrap_saved_view_source


RAW_TABLE = """
<table id="authorDashboardQueue">
  <thead><tr><th>Status</th><th>ID</th><th>Title</th><th>Submitted</th></tr></thead>
  <tr id="queue_0">
    <td data-label="status"><span class="pagecontents"> Under Review </span></td>
    <td data-label="ID"> AP2606-0001 (LONG-INTERNAL-ID) </td>
    <td data-label="title"> A   Complete Title <br><a>View Submission</a></td>
    <td data-label="submitted"> 09-Jul-2026 </td>
  </tr>
</table>
"""


def test_parse_dashboard_handles_direct_rows_and_normalizes_fields() -> None:
    assert main.parse_dashboard(RAW_TABLE) == (
        main.ManuscriptSnapshot(
            external_id="AP2606-0001",
            title="A Complete Title",
            submitted_text="09-Jul-2026",
            submitted_date=main.date(2026, 7, 9),
            status="Under Review",
        ),
    )


def test_user_fixture_unwraps_into_real_dashboard_html() -> None:
    snapshots = main.parse_dashboard(unwrap_saved_view_source(Path("demo.html")))
    assert snapshots == (
        main.ManuscriptSnapshot(
            external_id="AP2606-1127",
            title="RadioUNet: We are the RadioMapseer dataset",
            submitted_text="09-Jul-2026",
            submitted_date=main.date(2026, 7, 9),
            status="Awaiting Track Editor Assignment",
        ),
    )


def test_missing_dashboard_table_is_not_an_empty_dashboard() -> None:
    with pytest.raises(main.DashboardParseError, match="authorDashboardQueue"):
        main.parse_dashboard("<html><body>Login failed</body></html>")
```

- [ ] **Step 2: Run parser tests and verify RED**

Run:

```bash
uv run pytest tests/test_parser.py -v
```

Expected: FAIL because `ManuscriptSnapshot` or `parse_dashboard` is absent.

- [ ] **Step 3: Implement locale-independent parsing without `lxml`**

Add:

```python
ENGLISH_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


class DashboardParseError(ValueError):
    pass


def parse_submitted_date(value: str) -> date:
    parts = normalize_whitespace(value).split("-")
    if len(parts) != 3 or parts[1] not in ENGLISH_MONTHS:
        raise DashboardParseError("Submission date must use DD-Mon-YYYY.")
    try:
        return date(int(parts[2]), ENGLISH_MONTHS[parts[1]], int(parts[0]))
    except ValueError as exc:
        raise DashboardParseError("Submission date is invalid.") from exc
```

Implement row selection so real Selenium DOM and the approved saved source both work:

```python
def _dashboard_rows(table: Tag) -> list[Tag]:
    tbody = table.find("tbody", recursive=False)
    if isinstance(tbody, Tag):
        return [
            row
            for row in tbody.find_all("tr", recursive=False)
            if isinstance(row, Tag)
        ]

    direct_rows = [
        row for row in table.find_all("tr", recursive=False) if isinstance(row, Tag)
    ]
    has_manuscript_row = any(
        row.find("td", attrs={"data-label": True}, recursive=False) is not None
        for row in direct_rows
    )
    if not has_manuscript_row:
        raise DashboardParseError("Dashboard row structure is missing.")
    return direct_rows
```

For each row, require direct `status`, `ID`, `title`, and `submitted` cells. Find `span.pagecontents` inside the status cell. Clone the title cell with `BeautifulSoup(str(title_cell), "html.parser")`, call `decompose()` on every anchor, then normalize `get_text(" ", strip=True)`. Use the normalized short ID as the duplicate key. Error messages identify only the row number or safe short ID, never raw HTML. Return page order unchanged.

- [ ] **Step 4: Add malformed-page cases and verify GREEN**

Add parameterized tests for a valid empty `tbody`, nested status markup, two rows,
missing/empty cells, invalid dates, and duplicate normalized IDs. A table without `tbody` is valid
only when it has at least one direct manuscript row. Assert that a no-`tbody` empty table and any
malformed direct row fail the whole account parse, including a malformed row that would later be
excluded by ID filtering.

Run:

```bash
uv run pytest tests/test_parser.py -v
uv run ruff check main.py tests/conftest.py tests/test_parser.py
uv run ruff format --check main.py tests/conftest.py tests/test_parser.py
```

Expected: all parser cases pass, including the ignored user fixture.

- [ ] **Step 5: Commit only the production parser**

```bash
git add main.py
git diff --cached --check
git commit -m "feat: parse complete ScholarOne manuscript records"
```

Expected: `demo.html` and all parser tests remain ignored and unstaged.

---

### Task 3: SQLite Schema, Constraints, and Interrupted-Run Recovery

**Files:**
- Modify: `main.py`
- Test locally only: `tests/test_database.py`

**Interfaces:**
- Produces: `DatabaseError`.
- Produces: `connect_database(path: Path) -> sqlite3.Connection`.
- Produces: `migrate_database(conn: sqlite3.Connection) -> None`.
- Produces: `recover_interrupted_checks(conn: sqlite3.Connection, recovered_at: datetime) -> int`.
- Produces: `utc_text(value: datetime) -> str`.

- [ ] **Step 1: Write failing schema and recovery tests**

Create ignored `tests/test_database.py`:

```python
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import main


EXPECTED_TABLES = {
    "account_configuration_targets",
    "account_configurations",
    "accounts",
    "checks",
    "current_states",
    "manuscripts",
    "notification_batches",
    "notification_deliveries",
    "observations",
    "tracking_periods",
}


def test_migrate_database_creates_schema_v1_with_foreign_keys(tmp_path: Path) -> None:
    connection = main.connect_database(tmp_path / "nested/submissions.db")
    main.migrate_database(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert EXPECTED_TABLES <= tables
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_recover_interrupted_checks_marks_running_rows_failed(tmp_path: Path) -> None:
    connection = main.connect_database(tmp_path / "submissions.db")
    main.migrate_database(connection)
    now = "2026-07-10T00:00:00Z"
    account_id = connection.execute(
        "INSERT INTO accounts(name, active, needs_initial_notification, created_at, updated_at) "
        "VALUES ('primary', 1, 1, ?, ?)",
        (now, now),
    ).lastrowid
    configuration_id = connection.execute(
        "INSERT INTO account_configurations(account_id, url, username, track_all, "
        "active_from) VALUES (?, 'https://example.test', 'user', 1, ?)",
        (account_id, now),
    ).lastrowid
    connection.execute(
        "INSERT INTO checks(configuration_id, started_at, outcome) "
        "VALUES (?, ?, 'running')",
        (configuration_id, now),
    )
    connection.commit()

    recovered = main.recover_interrupted_checks(
        connection,
        datetime(2026, 7, 10, 1, tzinfo=timezone.utc),
    )
    connection.close()
    connection = main.connect_database(tmp_path / "submissions.db")
    row = connection.execute(
        "SELECT outcome, error_type FROM checks"
    ).fetchone()
    assert recovered == 1
    assert tuple(row) == ("failed", "InterruptedRun")
```

- [ ] **Step 2: Run schema tests and verify RED**

Run:

```bash
uv run pytest tests/test_database.py -v
```

Expected: FAIL because database functions are absent.

- [ ] **Step 3: Implement schema v1 with database-enforced ownership**

Use this schema as one `SCHEMA_V1` string and set `PRAGMA user_version = 1` at the end:

```sql
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    needs_initial_notification INTEGER NOT NULL
        CHECK (needs_initial_notification IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE account_configurations (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    url TEXT NOT NULL CHECK (length(trim(url)) > 0),
    username TEXT NOT NULL CHECK (length(trim(username)) > 0),
    track_all INTEGER NOT NULL CHECK (track_all IN (0, 1)),
    active_from TEXT NOT NULL,
    active_until TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX account_configurations_one_active
    ON account_configurations(account_id) WHERE active_until IS NULL;

CREATE TABLE account_configuration_targets (
    configuration_id INTEGER NOT NULL,
    external_id TEXT NOT NULL CHECK (length(trim(external_id)) > 0),
    PRIMARY KEY (configuration_id, external_id),
    FOREIGN KEY (configuration_id)
        REFERENCES account_configurations(id) ON DELETE RESTRICT
);

CREATE TABLE checks (
    id INTEGER PRIMARY KEY,
    configuration_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('running', 'succeeded', 'failed')),
    parsed_count INTEGER CHECK (parsed_count IS NULL OR parsed_count >= 0),
    error_type TEXT,
    error_message TEXT,
    CHECK (
        (outcome = 'running' AND completed_at IS NULL)
        OR (outcome <> 'running' AND completed_at IS NOT NULL)
    ),
    FOREIGN KEY (configuration_id)
        REFERENCES account_configurations(id) ON DELETE RESTRICT
);

CREATE TABLE manuscripts (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    external_id TEXT NOT NULL CHECK (length(trim(external_id)) > 0),
    created_at TEXT NOT NULL,
    first_seen_at TEXT,
    UNIQUE (account_id, external_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT
);

CREATE TABLE tracking_periods (
    id INTEGER PRIMARY KEY,
    manuscript_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    started_reason TEXT NOT NULL CHECK (
        started_reason IN (
            'account_activation',
            'filter_addition',
            'track_all_discovery',
            'scope_reactivation'
        )
    ),
    ended_reason TEXT CHECK (
        ended_reason IS NULL
        OR ended_reason IN ('account_removal', 'filter_removal')
    ),
    CHECK (
        (ended_at IS NULL AND ended_reason IS NULL)
        OR (ended_at IS NOT NULL AND ended_reason IS NOT NULL)
    ),
    UNIQUE (id, manuscript_id),
    FOREIGN KEY (manuscript_id) REFERENCES manuscripts(id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX tracking_periods_one_active
    ON tracking_periods(manuscript_id) WHERE ended_at IS NULL;

CREATE TABLE observations (
    id INTEGER PRIMARY KEY,
    check_id INTEGER NOT NULL,
    manuscript_id INTEGER NOT NULL,
    tracking_period_id INTEGER NOT NULL,
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    title TEXT,
    submitted_text TEXT,
    submitted_date TEXT,
    status TEXT,
    observed_at TEXT NOT NULL,
    CHECK (
        (present = 1 AND title IS NOT NULL AND submitted_text IS NOT NULL
            AND submitted_date IS NOT NULL AND status IS NOT NULL)
        OR (present = 0 AND title IS NULL AND submitted_text IS NULL
            AND submitted_date IS NULL AND status IS NULL)
    ),
    UNIQUE (check_id, tracking_period_id),
    UNIQUE (id, tracking_period_id),
    FOREIGN KEY (check_id) REFERENCES checks(id) ON DELETE RESTRICT,
    FOREIGN KEY (manuscript_id) REFERENCES manuscripts(id) ON DELETE RESTRICT,
    FOREIGN KEY (tracking_period_id, manuscript_id)
        REFERENCES tracking_periods(id, manuscript_id) ON DELETE RESTRICT
);

CREATE TABLE current_states (
    tracking_period_id INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (tracking_period_id)
        REFERENCES tracking_periods(id) ON DELETE RESTRICT,
    FOREIGN KEY (observation_id, tracking_period_id)
        REFERENCES observations(id, tracking_period_id) ON DELETE RESTRICT
);

CREATE TABLE notification_batches (
    id INTEGER PRIMARY KEY,
    check_id INTEGER NOT NULL UNIQUE,
    account_id INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('initial_verification', 'manuscript_changes')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    event_count INTEGER NOT NULL CHECK (event_count >= 0),
    created_at TEXT NOT NULL,
    committed_at TEXT,
    FOREIGN KEY (check_id) REFERENCES checks(id) ON DELETE RESTRICT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT
);

CREATE TABLE notification_deliveries (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    destination_index INTEGER NOT NULL CHECK (destination_index >= 0),
    scheme TEXT NOT NULL CHECK (length(trim(scheme)) > 0),
    attempted_at TEXT NOT NULL,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    error_type TEXT,
    error_message TEXT,
    UNIQUE (batch_id, destination_index),
    FOREIGN KEY (batch_id)
        REFERENCES notification_batches(id) ON DELETE RESTRICT
);

CREATE TRIGGER observations_account_matches_check
BEFORE INSERT ON observations
FOR EACH ROW
WHEN (
    SELECT account_configurations.account_id
    FROM checks
    JOIN account_configurations
        ON account_configurations.id = checks.configuration_id
    WHERE checks.id = NEW.check_id
) <> (
    SELECT manuscripts.account_id
    FROM manuscripts
    WHERE manuscripts.id = NEW.manuscript_id
)
BEGIN
    SELECT RAISE(ABORT, 'observation account does not match check');
END;

CREATE TRIGGER notification_batch_account_matches_check
BEFORE INSERT ON notification_batches
FOR EACH ROW
WHEN NEW.account_id <> (
    SELECT account_configurations.account_id
    FROM checks
    JOIN account_configurations
        ON account_configurations.id = checks.configuration_id
    WHERE checks.id = NEW.check_id
)
BEGIN
    SELECT RAISE(ABORT, 'notification batch account does not match check');
END;

PRAGMA user_version = 1;
```

Connect and migrate with explicit settings:

```python
SCHEMA_VERSION = 1


class DatabaseError(RuntimeError):
    pass


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required.")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = DELETE")
    return connection


def migrate_database(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise DatabaseError(
            f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}."
        )
    if version == 0:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_V1 + "\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise
```

`recover_interrupted_checks` runs inside `with conn:`, updates only `running` rows, sets the caller-supplied completion timestamp, `error_type = 'InterruptedRun'`, and a fixed English message without exception content. Its test closes and reopens the connection before asserting persistence.

- [ ] **Step 4: Verify schema constraints and GREEN**

Add tests that reject a second active configuration, a second active tracking period, cross-account observations, cross-account notification batches, mismatched current-state ownership, negative counts, and present observations with null content. Test that migration is idempotent and a future `user_version` raises `DatabaseError`. Inject an invalid statement into `SCHEMA_V1`, assert migration fails, reopen the database, and prove no partial business tables remain.

Run:

```bash
uv run pytest tests/test_database.py -v
uv run ruff check main.py tests/test_database.py
uv run ruff format --check main.py tests/test_database.py
```

Expected: all schema and recovery tests pass.

- [ ] **Step 5: Commit the schema without ignored tests**

```bash
git add main.py
git diff --cached --check
git commit -m "feat: add durable SQLite history schema"
```

---

### Task 4: Account Configuration Reconciliation and Tracking Scope

**Files:**
- Modify: `main.py`
- Test locally only: `tests/test_database.py`

**Interfaces:**
- Consumes: `AccountConfig`, SQLite schema, `utc_text`.
- Produces: `ReconciledAccount`.
- Produces: `reconcile_configuration(conn: sqlite3.Connection, accounts: Sequence[AccountConfig], now: datetime) -> dict[str, ReconciledAccount]`.

- [ ] **Step 1: Write reconciliation RED tests**

Add a local builder and one full history transition:

```python
def account(
    *,
    name: str = "primary",
    ids: tuple[str, ...] = ("AP2606-0001", "AP2606-0002"),
    url: str = "https://mc.manuscriptcentral.com/example",
) -> main.AccountConfig:
    return main.AccountConfig(
        name=name,
        url=url,
        username="author@example.com",
        password="secret",
        manuscript_ids=ids,
        apprise_urls=("json://localhost",),
    )


def test_reconcile_preserves_rows_across_filter_removal_and_reactivation(
    tmp_path: Path,
) -> None:
    conn = main.connect_database(tmp_path / "submissions.db")
    main.migrate_database(conn)
    first = datetime(2026, 7, 10, tzinfo=timezone.utc)

    initial = main.reconcile_configuration(conn, [account()], first)["primary"]
    assert initial.needs_initial_notification is True
    assert initial.target_ids == frozenset({"AP2606-0001", "AP2606-0002"})

    main.reconcile_configuration(
        conn,
        [account(ids=("AP2606-0002",))],
        first.replace(hour=1),
    )
    closed = conn.execute(
        "SELECT ended_reason FROM tracking_periods "
        "WHERE ended_reason IS NOT NULL ORDER BY id"
    ).fetchall()
    assert [row[0] for row in closed] == ["filter_removal"]

    main.reconcile_configuration(conn, [], first.replace(hour=2))
    restored = main.reconcile_configuration(
        conn,
        [account(ids=("AP2606-0001",))],
        first.replace(hour=3),
    )["primary"]

    assert restored.account_id == initial.account_id
    assert restored.configuration_id != initial.configuration_id
    assert restored.needs_initial_notification is True
    assert conn.execute("SELECT COUNT(*) FROM manuscripts").fetchone()[0] == 2
```

- [ ] **Step 2: Run reconciliation tests and verify RED**

Run:

```bash
uv run pytest tests/test_database.py -k reconcile -v
```

Expected: FAIL because `reconcile_configuration` is absent.

- [ ] **Step 3: Implement one-transaction reconciliation**

Add private helpers with these exact signatures:

```python
def _get_or_create_manuscript(
    conn: sqlite3.Connection,
    account_id: int,
    external_id: str,
    created_at: str,
) -> int:
    row = conn.execute(
        "SELECT id FROM manuscripts WHERE account_id = ? AND external_id = ?",
        (account_id, external_id),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO manuscripts(account_id, external_id, created_at) VALUES (?, ?, ?)",
        (account_id, external_id, created_at),
    )
    return int(cursor.lastrowid)


def _open_tracking_period(
    conn: sqlite3.Connection,
    manuscript_id: int,
    started_at: str,
    reason: str,
) -> int:
    row = conn.execute(
        "SELECT id FROM tracking_periods "
        "WHERE manuscript_id = ? AND ended_at IS NULL",
        (manuscript_id,),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO tracking_periods(manuscript_id, started_at, started_reason) "
        "VALUES (?, ?, ?)",
        (manuscript_id, started_at, reason),
    )
    return int(cursor.lastrowid)


def _close_active_periods(
    conn: sqlite3.Connection,
    account_id: int,
    ended_at: str,
    reason: str,
    *,
    keep_external_ids: AbstractSet[str] = frozenset(),
) -> None:
    rows = conn.execute(
        "SELECT tracking_periods.id, manuscripts.external_id "
        "FROM tracking_periods "
        "JOIN manuscripts ON manuscripts.id = tracking_periods.manuscript_id "
        "WHERE manuscripts.account_id = ? AND tracking_periods.ended_at IS NULL",
        (account_id,),
    ).fetchall()
    for row in rows:
        if row["external_id"] in keep_external_ids:
            continue
        conn.execute(
            "UPDATE tracking_periods SET ended_at = ?, ended_reason = ? WHERE id = ?",
            (ended_at, reason, row["id"]),
        )
```

Inside `with conn`, apply this order:

1. Mark database accounts absent from the configured name set inactive, set their activation-scoped `needs_initial_notification` flag to `0`, close their active revision, and close every active period with `account_removal`.
2. For each configured account in source order, reuse its stable `accounts.id`; on new or reactivated accounts set `active = 1` and `needs_initial_notification = 1`.
3. Compare the active revision's URL, username, `track_all`, and normalized target set. Passwords and destinations do not participate.
4. Reuse an identical revision. Otherwise close it and insert a new `account_configurations` row and its explicit targets.
5. If the desired mode is explicit, close active periods outside the target set with `filter_removal`, create missing manuscript rows, and open missing periods. Use `account_activation` for a new account, `scope_reactivation` for a reintroduced account, and `filter_addition` for an active-account addition.
6. If the desired mode is track-all, keep current active periods. New page-derived periods are created only after a complete parse in Task 5.
7. Return `ReconciledAccount` objects with the in-memory `AccountConfig`, so credentials never need database lookup.

Every SQL statement must use parameters. No branch runs `DELETE`, `INSERT OR REPLACE`, or a cascading conflict strategy.

- [ ] **Step 4: Add configuration-change cases and verify GREEN**

Test new accounts, identical reruns, URL/username revisions, password-only and destination-only reuse, explicit additions, all-to-explicit, explicit-to-all, account removal, reactivation, and an empty account list. Assert an inactive row has `needs_initial_notification = 0`, reactivation resets it to `1`, and track-all accounts create no page-derived period before a successful parse.

Run:

```bash
uv run pytest tests/test_database.py -v
uv run ruff check main.py tests/test_database.py
uv run ruff format --check main.py tests/test_database.py
```

Expected: all reconciliation history remains queryable and no removal is classified as page disappearance.

- [ ] **Step 5: Commit reconciliation behavior**

```bash
git add main.py
git diff --cached --check
git commit -m "feat: reconcile account and manuscript tracking history"
```

---

### Task 5: State Differences, Observations, and Complete Notification Batches

**Files:**
- Modify: `main.py`
- Test locally only: `tests/test_diff.py`
- Test locally only: `tests/test_database.py`

**Interfaces:**
- Consumes: parsed snapshots, reconciled accounts, SQLite schema.
- Produces: `EventType`, `AcceptedState`, `ManuscriptEvent`, `NotificationMessage`, `ProjectionUpdate`, `StoredNotificationBatch`, `PreparedCheck`.
- Produces: `calculate_events(current: Mapping[str, ManuscriptSnapshot], accepted: Mapping[str, AcceptedState], *, initial_verification: bool) -> tuple[ManuscriptEvent, ...]`.
- Produces: `build_notification(account_name: str, checked_at: datetime, events: Sequence[ManuscriptEvent], *, initial_verification: bool) -> NotificationMessage | None`.
- Produces: `start_check`, `prepare_check`, `complete_check_without_notification`, and `fail_check` with the shared signatures.

- [ ] **Step 1: Write pure transition and complete-message RED tests**

Create ignored `tests/test_diff.py`:

```python
from datetime import date, datetime, timezone

import main


def snapshot(
    status: str = "Under Review",
    title: str = "Complete Title",
) -> main.ManuscriptSnapshot:
    return main.ManuscriptSnapshot(
        external_id="AP2606-0001",
        title=title,
        submitted_text="09-Jul-2026",
        submitted_date=date(2026, 7, 9),
        status=status,
    )


def accepted(value: main.ManuscriptSnapshot | None, *, present: bool) -> main.AcceptedState:
    return main.AcceptedState(
        tracking_period_id=10,
        observation_id=20,
        present=present,
        snapshot=value,
    )


def test_status_change_contains_previous_and_current_complete_values() -> None:
    before = snapshot("Under Review")
    after = snapshot("Accepted")
    events = main.calculate_events(
        {after.external_id: after},
        {before.external_id: accepted(before, present=True)},
        initial_verification=False,
    )
    message = main.build_notification(
        "primary",
        datetime(2026, 7, 10, tzinfo=timezone.utc),
        events,
        initial_verification=False,
    )

    assert [event.kind for event in events] == [main.EventType.STATUS_CHANGED]
    assert message is not None
    assert "Previous status: Under Review" in message.body
    assert "Current status: Accepted" in message.body
    assert "ID: AP2606-0001" in message.body
    assert "Title: Complete Title" in message.body


def test_initial_empty_scope_still_builds_verification_message() -> None:
    message = main.build_notification(
        "primary",
        datetime(2026, 7, 10, tzinfo=timezone.utc),
        (),
        initial_verification=True,
    )
    assert message is not None
    assert message.reason == "initial_verification"
    assert message.event_count == 0
    assert "No manuscripts were found" in message.body
```

- [ ] **Step 2: Run difference tests and verify RED**

Run:

```bash
uv run pytest tests/test_diff.py -v
```

Expected: FAIL because transition and notification types/functions are absent.

- [ ] **Step 3: Implement deterministic pure event calculation**

Implement this decision order for every sorted ID in `set(current) | set(accepted)`:

```python
if initial_verification and current_snapshot is not None:
    kind = EventType.CURRENT
elif previous is None and current_snapshot is not None:
    kind = EventType.NEW
elif previous is not None and previous.present and current_snapshot is None:
    kind = EventType.DISAPPEARED
elif previous is not None and not previous.present and current_snapshot is not None:
    kind = EventType.REAPPEARED
elif (
    previous is not None
    and previous.present
    and previous.snapshot is not None
    and current_snapshot is not None
    and previous.snapshot.status != current_snapshot.status
):
    kind = EventType.STATUS_CHANGED
else:
    continue
```

Create events with full snapshots and never compare titles or dates for event generation. `build_notification` returns `None` only when there are no events and initial verification is false. Render plain text in stable ID order with account and UTC check time, blank lines between manuscripts, and these exact labels: `Event`, `ID`, `Title`, `Submitted`, `Status`, `Previous status`, `Current status`, and `Last known status`. Never slice the body or title.

- [ ] **Step 4: Write failing SQLite check-lifecycle tests**

Add integration tests that call `reconcile_configuration`, `start_check`, and `prepare_check` against a real temporary database. Cover:

- Initial populated check creates `CURRENT` events and only deferred updates.
- Initial empty check creates a zero-event batch and no state.
- Track-all creates periods only for present rows after a valid parse.
- Explicit never-seen targets add absent observations but no event/state.
- An unchanged accepted snapshot completes without a batch.
- Title/date-only changes update `current_states` immediately.
- A status change for manuscript A and title-only change for B defers A but immediately advances B.
- A committed absence followed by presence produces `REAPPEARED`.

For the mixed case, use two concrete IDs (`AP2606-0001` for the status change and
`AP2606-0002` for the title change). Query their real period IDs by joining `tracking_periods` to
`manuscripts`. Assert the prepared event/deferred-update sets contain only the first ID/period.
Then compare the second period's `current_states.observation_id` with `MAX(observations.id)` for
that period; they must match immediately. Compare the first period's projection before and after
preparation; it must remain at the accepted observation until delivery succeeds. Do not introduce
test helpers that return hard-coded observation or period IDs.

- [ ] **Step 5: Implement check preparation with immediate and deferred projections**

Use the exact `prepare_check(conn, account, check_id, parsed, observed_at) -> PreparedCheck` interface from Shared Interfaces and implement these complete lifecycle helpers around it:

```python
def start_check(
    conn: sqlite3.Connection,
    configuration_id: int,
    started_at: datetime,
) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO checks(configuration_id, started_at, outcome) "
            "SELECT id, ?, 'running' FROM account_configurations "
            "WHERE id = ? AND active_until IS NULL",
            (utc_text(started_at), configuration_id),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("The active account configuration was not found.")
    return int(cursor.lastrowid)


def complete_check_without_notification(
    conn: sqlite3.Connection,
    prepared: PreparedCheck,
    completed_at: datetime,
) -> None:
    if prepared.batch is not None:
        raise DatabaseError("A notification batch requires delivery finalization.")
    with conn:
        cursor = conn.execute(
            "UPDATE checks SET completed_at = ?, outcome = 'succeeded', parsed_count = ?, "
            "error_type = NULL, error_message = NULL "
            "WHERE id = ? AND configuration_id = ? AND outcome = 'running'",
            (
                utc_text(completed_at),
                prepared.parsed_count,
                prepared.check_id,
                prepared.configuration_id,
            ),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("The prepared check is no longer running.")


def fail_check(
    conn: sqlite3.Connection,
    check_id: int,
    completed_at: datetime,
    error: RedactedError,
) -> None:
    with conn:
        cursor = conn.execute(
            "UPDATE checks SET completed_at = ?, outcome = 'failed', error_type = ?, "
            "error_message = ? WHERE id = ? AND outcome = 'running'",
            (utc_text(completed_at), error.error_type, error.error_message, check_id),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("The check is no longer running.")
```

`prepare_check` performs one short transaction:

1. Verify `check_id` is still `running`, belongs to `account.configuration_id`, and that configuration belongs to `account.account_id`; otherwise raise `DatabaseError` before inserting anything.
2. Store `parsed_count = len(parsed)` before filtering.
3. In track-all mode, create manuscript rows and `track_all_discovery` periods for present parsed IDs without active periods.
4. In explicit mode, retain only configured IDs after the complete parse.
5. Load every active period and its accepted state.
6. Insert one present or absent observation per active period. Set `first_seen_at` only on first presence.
7. Calculate events from scoped present snapshots and accepted states.
8. Upsert `current_states` immediately for observations without events when a prior state or current snapshot exists. Never create a state for a never-seen absence.
9. Collect event observation pointers as `deferred_updates`.
10. Build and insert one `notification_batches` row for events or pending initial verification.
11. Return an immutable `PreparedCheck`; do not mark its check complete yet.

`complete_check_without_notification` rejects a prepared check that has a batch, then marks it succeeded. `fail_check` marks browser/parser failures with the supplied redacted data. All timestamps come from the caller.

- [ ] **Step 6: Verify every transition and GREEN**

Add parameterized pure cases for all five events, repeated absence, unchanged status, title/date-only changes, deterministic order, and long untruncated content. Add stale/wrong-configuration tests for `prepare_check`, `complete_check_without_notification`, and `fail_check`; every invalid lifecycle call must raise `DatabaseError` and leave rows unchanged. Run:

```bash
uv run pytest tests/test_diff.py tests/test_database.py -v
uv run ruff check main.py tests/test_diff.py tests/test_database.py
uv run ruff format --check main.py tests/test_diff.py tests/test_database.py
```

Expected: pure and SQLite-backed transition tests pass.

- [ ] **Step 7: Commit state preparation and complete messages**

```bash
git add main.py
git diff --cached --check
git commit -m "feat: persist manuscript observations and state changes"
```

---

### Task 6: Per-Destination Apprise Delivery and Transaction Finalization

**Files:**
- Modify: `main.py`
- Test locally only: `tests/test_notifications.py`
- Test locally only: `tests/test_database.py`

**Interfaces:**
- Consumes: `NotificationMessage`, `PreparedCheck`, configured Apprise URLs.
- Produces: `RedactedError`, `DeliveryResult`.
- Produces: `extract_apprise_scheme(url: str) -> str`.
- Produces: `redact_exception(exc: BaseException, secrets: Iterable[str] = ()) -> RedactedError`.
- Produces: `deliver_notifications(destinations, title, body, *, apprise_factory, clock) -> tuple[DeliveryResult, ...]`.
- Produces: `record_delivery`, `commit_prepared_check`, and `fail_prepared_check` with the shared signatures.

- [ ] **Step 1: Write failing independent-delivery tests**

Create ignored `tests/test_notifications.py`:

```python
import logging
from datetime import datetime, timezone

import main


class FakeApprise:
    outcomes = iter(())
    instances: list["FakeApprise"] = []

    def __init__(self) -> None:
        self.url: str | None = None
        self.body: str | None = None
        self.title: str | None = None
        self.instances.append(self)

    def add(self, url: str) -> bool:
        self.url = url
        return True

    def notify(self, *, title: str, body: str) -> bool:
        self.title = title
        self.body = body
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            logging.getLogger("apprise.plugin").critical("%s", outcome)
            raise outcome
        return bool(outcome)


def test_delivery_attempts_every_url_and_preserves_complete_body() -> None:
    FakeApprise.instances = []
    FakeApprise.outcomes = iter((True, False))
    body = "Complete body that must not be truncated"

    results = main.deliver_notifications(
        ("json://first", "json://second"),
        "ScholarOne update",
        body,
        apprise_factory=FakeApprise,
        clock=lambda: datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    assert [result.success for result in results] == [True, False]
    assert len(FakeApprise.instances) == 2
    assert all(instance.body == body for instance in FakeApprise.instances)
    assert "json://" not in repr(results)


def test_delivery_exception_and_critical_library_log_are_suppressed(caplog) -> None:
    FakeApprise.instances = []
    FakeApprise.outcomes = iter((RuntimeError("failed for decoded credential fragment"),))
    previous_disable_level = logging.root.manager.disable
    results = main.deliver_notifications(
        ("json://token@example.invalid/path",),
        "Title",
        "Body",
        apprise_factory=FakeApprise,
        clock=lambda: datetime(2026, 7, 10, tzinfo=timezone.utc),
    )
    assert results[0].success is False
    assert results[0].error_message == "Apprise delivery raised an exception."
    assert "credential fragment" not in repr(results)
    assert "credential fragment" not in caplog.text
    assert logging.root.manager.disable == previous_disable_level
```

- [ ] **Step 2: Run notification tests and verify RED**

Run:

```bash
uv run pytest tests/test_notifications.py -v
```

Expected: FAIL because delivery functions are absent.

- [ ] **Step 3: Implement secret-safe Apprise delivery**

Use one fresh Apprise object for every URL and continue after every failure:

```python
def extract_apprise_scheme(url: str) -> str:
    candidate = urlsplit(url).scheme.lower()
    return candidate if re.fullmatch(r"[a-z][a-z0-9+.-]*", candidate) else "unknown"


def redact_exception(
    exc: BaseException,
    secrets: Iterable[str] = (),
) -> RedactedError:
    # Arbitrary exception text is never safe enough to persist after string replacement.
    del secrets
    return RedactedError(
        type(exc).__name__,
        "Operation failed; error details were suppressed.",
    )


def deliver_notifications(
    destinations: Sequence[str],
    title: str,
    body: str,
    *,
    apprise_factory: Callable[[], Any] = apprise.Apprise,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[DeliveryResult, ...]:
    results: list[DeliveryResult] = []
    for index, destination in enumerate(destinations):
        scheme = extract_apprise_scheme(destination)
        try:
            previous_disable_level = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            try:
                notifier = apprise_factory()
                if not notifier.add(destination):
                    results.append(
                        DeliveryResult(
                            index,
                            scheme,
                            clock(),
                            False,
                            "InvalidDestination",
                            "Apprise rejected this destination.",
                        )
                    )
                    continue
                success = bool(notifier.notify(title=title, body=body))
                results.append(
                    DeliveryResult(
                        index,
                        scheme,
                        clock(),
                        success,
                        None if success else "DeliveryFailed",
                        None if success else "Apprise reported a failed delivery.",
                    )
                )
            finally:
                logging.disable(previous_disable_level)
        except Exception as exc:
            results.append(
                DeliveryResult(
                    index,
                    scheme,
                    clock(),
                    False,
                    type(exc).__name__,
                    "Apprise delivery raised an exception.",
                )
            )
    return tuple(results)
```

Add tests for `add()` returning false, a thrown `add()`, false `notify()`, an exception after an
earlier success, and safe schemes. Make a fake log a credential fragment at `CRITICAL` before
raising from both `add()` and `notify()`; assert with `caplog` that the fragment never appears and
that the process-wide logging disable level is restored after every outcome. The fixed persisted
messages above, rather than replacement inside arbitrary exception strings, provide the redaction
guarantee.

- [ ] **Step 4: Write failing database finalization tests**

Extend the real-SQLite tests:

```python
for result in results:
    main.record_delivery(conn, prepared.batch.id, result)

main.commit_prepared_check(conn, prepared, committed_at=now)

assert conn.execute(
    "SELECT committed_at FROM notification_batches WHERE id = ?",
    (prepared.batch.id,),
).fetchone()[0] is not None
assert conn.execute(
    "SELECT outcome FROM checks WHERE id = ?",
    (prepared.check_id,),
).fetchone()[0] == "succeeded"
```

Add a total-failure case that calls `fail_prepared_check` and proves deferred states and `needs_initial_notification` remain unchanged while observations, safe projections, batch, and delivery rows remain queryable.

- [ ] **Step 5: Implement delivery persistence and guarded commit**

Use these signatures:

```python
def record_delivery(
    conn: sqlite3.Connection,
    batch_id: int,
    result: DeliveryResult,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO notification_deliveries("
            "batch_id, destination_index, scheme, attempted_at, success, error_type, error_message"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                result.destination_index,
                result.scheme,
                utc_text(result.attempted_at),
                int(result.success),
                result.error_type,
                result.error_message,
            ),
        )


def commit_prepared_check(
    conn: sqlite3.Connection,
    prepared: PreparedCheck,
    committed_at: datetime,
) -> None:
    if prepared.batch is None:
        raise DatabaseError("No notification batch is available to commit.")
    timestamp = utc_text(committed_at)
    with conn:
        succeeded = conn.execute(
            "SELECT 1 FROM notification_batches "
            "JOIN notification_deliveries "
            "ON notification_deliveries.batch_id = notification_batches.id "
            "WHERE notification_batches.id = ? "
            "AND notification_batches.check_id = ? "
            "AND notification_batches.account_id = ? "
            "AND notification_batches.committed_at IS NULL "
            "AND notification_deliveries.success = 1 LIMIT 1",
            (prepared.batch.id, prepared.check_id, prepared.account_id),
        ).fetchone()
        if succeeded is None:
            raise DatabaseError(
                "An uncommitted matching batch with a successful delivery is required."
            )
        batch_cursor = conn.execute(
            "UPDATE notification_batches SET committed_at = ? "
            "WHERE id = ? AND check_id = ? AND account_id = ? AND committed_at IS NULL",
            (timestamp, prepared.batch.id, prepared.check_id, prepared.account_id),
        )
        if batch_cursor.rowcount != 1:
            raise DatabaseError("The notification batch is stale or mismatched.")
        period_ids = [update.tracking_period_id for update in prepared.deferred_updates]
        if len(period_ids) != len(set(period_ids)):
            raise DatabaseError("Deferred projection periods must be unique.")
        for update in prepared.deferred_updates:
            owned = conn.execute(
                "SELECT 1 FROM observations "
                "JOIN tracking_periods "
                "ON tracking_periods.id = observations.tracking_period_id "
                "JOIN manuscripts ON manuscripts.id = tracking_periods.manuscript_id "
                "WHERE observations.id = ? AND observations.tracking_period_id = ? "
                "AND observations.check_id = ? AND manuscripts.account_id = ?",
                (
                    update.observation_id,
                    update.tracking_period_id,
                    prepared.check_id,
                    prepared.account_id,
                ),
            ).fetchone()
            if owned is None:
                raise DatabaseError("A deferred projection is stale or belongs to another check.")
            state_cursor = conn.execute(
                "INSERT INTO current_states(tracking_period_id, observation_id, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(tracking_period_id) DO UPDATE SET "
                "observation_id = excluded.observation_id, updated_at = excluded.updated_at",
                (update.tracking_period_id, update.observation_id, timestamp),
            )
            if state_cursor.rowcount != 1:
                raise DatabaseError("A deferred projection could not be committed.")
        if prepared.clear_initial_on_commit:
            conn.execute(
                "UPDATE accounts SET needs_initial_notification = 0, updated_at = ? "
                "WHERE id = ?",
                (timestamp, prepared.account_id),
            )
        check_cursor = conn.execute(
            "UPDATE checks SET completed_at = ?, outcome = 'succeeded', parsed_count = ?, "
            "error_type = NULL, error_message = NULL "
            "WHERE id = ? AND configuration_id = ? AND outcome = 'running'",
            (
                timestamp,
                prepared.parsed_count,
                prepared.check_id,
                prepared.configuration_id,
            ),
        )
        if check_cursor.rowcount != 1:
            raise DatabaseError("The prepared check is stale or mismatched.")


def fail_prepared_check(
    conn: sqlite3.Connection,
    prepared: PreparedCheck,
    completed_at: datetime,
    error: RedactedError,
) -> None:
    if prepared.batch is None:
        raise DatabaseError("No notification batch is available to fail.")
    with conn:
        batch = conn.execute(
            "SELECT 1 FROM notification_batches "
            "WHERE id = ? AND check_id = ? AND account_id = ? AND committed_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM notification_deliveries "
            "WHERE batch_id = notification_batches.id AND success = 1)",
            (prepared.batch.id, prepared.check_id, prepared.account_id),
        ).fetchone()
        if batch is None:
            raise DatabaseError("Only an uncommitted batch without a success may fail.")
        cursor = conn.execute(
            "UPDATE checks SET completed_at = ?, outcome = 'failed', parsed_count = ?, "
            "error_type = ?, error_message = ? "
            "WHERE id = ? AND configuration_id = ? AND outcome = 'running'",
            (
                utc_text(completed_at),
                prepared.parsed_count,
                error.error_type,
                error.error_message,
                prepared.check_id,
                prepared.configuration_id,
            ),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("The prepared check is stale or mismatched.")
```

`record_delivery` commits one attempt independently. `commit_prepared_check` must reject a missing batch and verify this query before updating anything:

```sql
SELECT 1
FROM notification_deliveries
WHERE batch_id = ? AND success = 1
LIMIT 1;
```

In one transaction, require an uncommitted batch that belongs to the prepared check/account, set
`notification_batches.committed_at`, and reject duplicate deferred periods. Before each
`current_states` upsert, verify its observation belongs to the prepared check and its period's
manuscript belongs to the prepared account; require one affected row. Then clear
`accounts.needs_initial_notification` only when `clear_initial_on_commit` is true and update exactly
one matching running check. `fail_prepared_check` requires the same ownership, requires that no
successful delivery exists, leaves all deferred pointers and the initial flag alone, and updates
exactly one matching running check as failed. A zero-row update or mismatched/repeated finalization
raises `DatabaseError` and rolls back. Never reuse an old uncommitted batch on a later run.

- [ ] **Step 6: Verify notification semantics and GREEN**

Run:

```bash
uv run pytest tests/test_notifications.py tests/test_database.py tests/test_diff.py -v
uv run ruff check main.py tests/test_notifications.py tests/test_database.py tests/test_diff.py
uv run ruff format --check main.py tests/test_notifications.py \
  tests/test_database.py tests/test_diff.py
```

Expected: partial success commits, total failure retains only deferred baselines, and no recorded
value contains a complete destination. Add tests that repeated commits, wrong prepared IDs,
already recovered checks, committed batches, duplicate deferred periods, foreign-check deferred
observations, cross-account deferred periods, and failing a batch with a persisted success all
raise `DatabaseError` and roll back every batch, state, account-flag, and check mutation.

- [ ] **Step 7: Commit Apprise delivery behavior**

```bash
git add main.py
git diff --cached --check
git commit -m "feat: deliver state changes through Apprise"
```

---

### Task 7: Cross-Platform Selenium Capture and Guaranteed Cleanup

**Files:**
- Modify: `main.py`
- Test locally only: `tests/test_browser.py`

**Interfaces:**
- Consumes: `BrowserConfig`, `AccountConfig`, `normalize_whitespace`.
- Produces: `BrowserCaptureError`.
- Produces: `create_chrome_driver(config: BrowserConfig, environ: Mapping[str, str]) -> Any`.
- Produces: `capture_dashboard_html(account: AccountConfig, browser: BrowserConfig, *, environ, driver_factory, wait_factory) -> str`.

- [ ] **Step 1: Write driver precedence and cleanup RED tests**

Create ignored `tests/test_browser.py` with monkeypatched `webdriver.Chrome` and `Service`. Use a
real `ChromeOptions` value without starting a browser:

```python
from pathlib import Path

import main


def test_configured_driver_wins_and_docker_flags_are_added(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeService:
        def __init__(self, *, executable_path: str) -> None:
            captured["service_path"] = executable_path

    class FakeDriver:
        def set_page_load_timeout(self, seconds: int) -> None:
            captured["timeout"] = seconds

        def quit(self) -> None:
            captured["quit"] = True

    def fake_chrome(**kwargs: object) -> FakeDriver:
        options = kwargs["options"]
        captured["arguments"] = tuple(options.arguments)
        captured["binary_path"] = options.binary_location
        return FakeDriver()

    monkeypatch.setattr(main, "Service", FakeService)
    monkeypatch.setattr(main.webdriver, "Chrome", fake_chrome)
    config = main.BrowserConfig(
        headless=True,
        element_timeout_seconds=30,
        page_load_timeout_seconds=60,
        binary_path=None,
        driver_path=Path("configured-driver"),
    )

    main.create_chrome_driver(config, {
        "CHROMEDRIVER_PATH": "environment-driver",
        "CHROME_BIN": "environment-browser",
        "RUNNING_IN_DOCKER": "1",
    })

    assert captured["service_path"] == "configured-driver"
    assert captured["binary_path"] == "environment-browser"
    assert "--no-sandbox" in captured["arguments"]
    assert "--disable-dev-shm-usage" in captured["arguments"]
    assert captured["timeout"] == 60
```

Add a driver whose `set_page_load_timeout()` raises and assert `create_chrome_driver()` calls its
`quit()` exactly once before propagating. Use a fake driver/wait sequence to assert
`capture_dashboard_html` returns the exact `page_source`, executes a JavaScript `Author` href,
and calls `quit()` exactly once. Parameterize failures at `get`, username, password, login,
Author, table wait, and page source and assert cleanup in every case. A driver-factory failure has
no cleanup obligation because construction either produced no driver or cleaned it internally.

- [ ] **Step 2: Run browser tests and verify RED**

Run:

```bash
uv run pytest tests/test_browser.py -v
```

Expected: FAIL because browser functions are absent.

- [ ] **Step 3: Implement Selenium Manager fallback and Docker-only flags**

Implement driver precedence without platform conditionals:

```python
def create_chrome_driver(
    config: BrowserConfig,
    environ: Mapping[str, str],
) -> Any:
    options = webdriver.ChromeOptions()
    if config.headless:
        options.add_argument("--headless=new")
    binary = config.binary_path or (
        Path(environ["CHROME_BIN"]) if environ.get("CHROME_BIN") else None
    )
    if binary is not None:
        options.binary_location = str(binary)
    if environ.get("RUNNING_IN_DOCKER") == "1":
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    driver_path = config.driver_path or (
        Path(environ["CHROMEDRIVER_PATH"])
        if environ.get("CHROMEDRIVER_PATH")
        else None
    )
    kwargs: dict[str, Any] = {"options": options}
    if driver_path is not None:
        kwargs["service"] = Service(executable_path=str(driver_path))
    driver = webdriver.Chrome(**kwargs)
    try:
        driver.set_page_load_timeout(config.page_load_timeout_seconds)
    except Exception:
        try:
            driver.quit()
        except Exception:
            LOGGER.warning("Browser cleanup failed during driver initialization.")
        raise
    return driver
```

When no driver path is supplied, omit `Service`; Selenium Manager is then the official fallback.

- [ ] **Step 4: Implement explicit login/navigation waits and cleanup**

Wrap account-local browser failures in a fixed safe exception and use these defaults and sequence:

```python
class BrowserCaptureError(RuntimeError):
    pass


def capture_dashboard_html(
    account: AccountConfig,
    browser: BrowserConfig,
    *,
    environ: Mapping[str, str],
    driver_factory: Callable[[BrowserConfig, Mapping[str, str]], Any] = create_chrome_driver,
    wait_factory: Callable[[Any, int], Any] = WebDriverWait,
) -> str:
    driver: Any | None = None
    try:
        driver = driver_factory(browser, environ)
        driver.get(account.url)
        wait = wait_factory(driver, browser.element_timeout_seconds)
        username = wait.until(EC.presence_of_element_located((By.NAME, "USERID")))
        username.clear()
        username.send_keys(account.username)
        password = wait.until(EC.presence_of_element_located((By.NAME, "PASSWORD")))
        password.clear()
        password.send_keys(account.password)
        login = wait.until(EC.element_to_be_clickable((By.ID, "logInButton")))
        driver.execute_script("arguments[0].click();", login)

        def find_author(current_driver: Any) -> Any:
            for link in current_driver.find_elements(By.CSS_SELECTOR, "li.nav-link a"):
                if normalize_whitespace(link.text) == "Author":
                    return link
            return False

        author = wait.until(find_author)
        href = author.get_attribute("href") or ""
        if href.lower().startswith("javascript:"):
            driver.execute_script(href.split(":", 1)[1])
        else:
            author.click()
        wait.until(EC.presence_of_element_located((By.ID, "authorDashboardQueue")))
        return driver.page_source
    except Exception as exc:
        raise BrowserCaptureError(
            f"Browser capture failed with {type(exc).__name__}."
        ) from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                LOGGER.warning("Browser cleanup failed for account %s.", account.name)
```

Do not log the exception representation from cleanup. Do not add random sleeps or a hard-coded user agent.

- [ ] **Step 5: Verify browser paths and GREEN**

Run:

```bash
uv run pytest tests/test_browser.py -v
uv run ruff check main.py tests/test_browser.py
uv run ruff format --check main.py tests/test_browser.py
```

Expected: path precedence, Selenium Manager fallback, explicit waits, JavaScript/click Author navigation, page capture, and cleanup all pass.

- [ ] **Step 6: Commit Selenium capture**

```bash
git add main.py
git diff --cached --check
git commit -m "feat: capture ScholarOne dashboards with Selenium"
```

---

### Task 8: One-Shot Multi-Account Workflow, File Lock, and CLI

**Files:**
- Modify: `main.py`
- Test locally only: `tests/test_workflow.py`

**Interfaces:**
- Consumes: every previous task interface.
- Produces: `check_account(conn, account, browser, *, environ, driver_factory, wait_factory, apprise_factory, clock) -> bool`.
- Produces: `run_once(config: AppConfig, *, environ, driver_factory, wait_factory, apprise_factory, clock) -> int`.
- Produces: `build_argument_parser() -> argparse.ArgumentParser`.
- Produces: `main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write end-to-end workflow RED tests with real temporary SQLite**

Create ignored `tests/test_workflow.py`. Build one concrete local harness in that file: a driver
factory consumes queued page-source strings or exceptions, a wait fake returns deterministic login
and Author elements, an Apprise factory records `(destination, title, body)` and consumes queued
boolean/exception outcomes, and a monotonically increasing UTC clock avoids timestamp collisions.
The harness must call the public `run_once` interface; it must not replace `check_account`, parsing,
reconciliation, or database finalization. Never open a real browser or network connection.

Cover these complete workflows with real temporary SQLite files:

- Seed an initial `Under Review` success, run `Accepted` with total delivery failure, then rerun
  `Accepted` with success. Assert exit codes `1` then `0`, two complete `STATUS_CHANGED` bodies,
  and an accepted projection only after the second run.
- Configure two accounts with distinct destinations and different manuscript rows. Change both in
  one invocation. Assert exactly two messages, each contains every changed manuscript for its own
  account, neither contains the other account's data, and each destination receives only its own
  message.
- Make capture fail for the first of two accounts and succeed for the second. Assert exit code `1`,
  a failed check for the first, and a succeeded check plus delivery for the second.
- Start a track-all account with one manuscript, commit the initial verification, then return the
  original row plus a second manuscript. Assert the next message contains exactly one `NEW` event
  for the newly discovered ID.
- Test initial populated and empty runs, partial destination success, disappearance retry,
  reappearance, filter/account removal, account reactivation as `CURRENT`, an explicit target's
  first appearance as `NEW`, title-only safe projection, empty account configuration, and
  interrupted-run recovery.
- Force `Path.mkdir`, `FileLock` construction, lock acquisition, and database connection failures
  independently. Assert `run_once` returns `1`, emits no secret, and leaves no running check.

- [ ] **Step 2: Run workflow tests and verify RED**

Run:

```bash
uv run pytest tests/test_workflow.py -v
```

Expected: FAIL because orchestration and CLI entry points are absent.

- [ ] **Step 3: Implement per-account orchestration without long transactions**

Use this flow:

```python
def check_account(
    conn: sqlite3.Connection,
    account: ReconciledAccount,
    browser: BrowserConfig,
    *,
    environ: Mapping[str, str],
    driver_factory: Callable[..., Any],
    wait_factory: Callable[..., Any],
    apprise_factory: Callable[[], Any],
    clock: Callable[[], datetime],
) -> bool:
    check_id = start_check(conn, account.configuration_id, clock())
    try:
        html = capture_dashboard_html(
            account.config,
            browser,
            environ=environ,
            driver_factory=driver_factory,
            wait_factory=wait_factory,
        )
        parsed = parse_dashboard(html)
        prepared = prepare_check(conn, account, check_id, parsed, clock())
    except (BrowserCaptureError, DashboardParseError) as exc:
        error = redact_exception(
            exc,
            (account.config.password, *account.config.apprise_urls),
        )
        fail_check(conn, check_id, clock(), error)
        LOGGER.error(
            "Account %s failed during capture or parsing: %s",
            account.config.name,
            error.error_type,
        )
        return False

    if prepared.batch is None:
        complete_check_without_notification(conn, prepared, clock())
        return True

    results = deliver_notifications(
        account.config.apprise_urls,
        prepared.batch.title,
        prepared.batch.body,
        apprise_factory=apprise_factory,
        clock=clock,
    )
    for result in results:
        record_delivery(conn, prepared.batch.id, result)
    if any(result.success for result in results):
        commit_prepared_check(conn, prepared, clock())
        return True
    fail_prepared_check(
        conn,
        prepared,
        clock(),
        RedactedError("NotificationDeliveryError", "Every Apprise destination failed."),
    )
    return False
```

Database exceptions during preparation/finalization are deliberately outside this account-local exception tuple and propagate to `run_once` as process-fatal failures.

- [ ] **Step 4: Implement the process lock and one-shot runner**

Use `<database_path>.lock` with immediate timeout:

```python
def run_once(
    config: AppConfig,
    *,
    environ: Mapping[str, str] | None = None,
    driver_factory: Callable[..., Any] = create_chrome_driver,
    wait_factory: Callable[..., Any] = WebDriverWait,
    apprise_factory: Callable[[], Any] = apprise.Apprise,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    runtime_environment = os.environ if environ is None else environ
    database_path = config.storage.database_path
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(f"{database_path}.lock", timeout=0)
        with lock:
            conn = connect_database(database_path)
            try:
                migrate_database(conn)
                recover_interrupted_checks(conn, clock())
                reconciled = reconcile_configuration(conn, config.accounts, clock())
                outcomes = [
                    check_account(
                        conn,
                        reconciled[account.name],
                        config.browser,
                        environ=runtime_environment,
                        driver_factory=driver_factory,
                        wait_factory=wait_factory,
                        apprise_factory=apprise_factory,
                        clock=clock,
                    )
                    for account in config.accounts
                ]
                return 0 if all(outcomes) else 1
            finally:
                conn.close()
    except FileLockTimeout:
        LOGGER.error("Another check is already using database %s.", database_path)
        return 1
    except (OSError, sqlite3.Error, DatabaseError):
        LOGGER.exception("The database operation failed.")
        return 1
```

An empty `outcomes` list succeeds after reconciliation because `all(())` is true.

- [ ] **Step 5: Implement CLI-only configuration error exit code 2**

```python
def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check ScholarOne manuscript statuses once and send Apprise notifications."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Path to config.toml (default: ./config.toml).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        return 2
    return run_once(config)


if __name__ == "__main__":
    raise SystemExit(main())
```

Importing `main` and invoking `--help` must create no database, lock, data directory, log file, or browser.

- [ ] **Step 6: Run the full ignored suite and verify GREEN**

Run:

```bash
uv run pytest -v
uv run ruff check main.py tests
uv run ruff format --check main.py tests
uv run python -m py_compile main.py
uv run python main.py --help
```

Expected: all tests pass, `--help` exits 0, and no runtime files appear.

- [ ] **Step 7: Commit the complete one-shot application**

```bash
git add main.py
git diff --cached --check
git commit -m "feat: run isolated multi-account checks"
```

---

### Task 9: Reproducible Docker Compose Scheduling and Cross-Platform Smoke CI

**Files:**
- Create: `.env.example`
- Create: `.dockerignore`
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.github/workflows/ci.yml`
- Test locally only: `tests/test_repository.py`

**Interfaces:**
- Consumes: one-shot `/app/.venv/bin/python /app/main.py --config /app/config.toml`.
- Produces: Compose service `checker`, named volume `submission-data`, and environment contract `CRON_SCHEDULE`, `TZ`, `RUNNING_IN_DOCKER`, `CHROME_BIN`, `CHROMEDRIVER_PATH`.

- [ ] **Step 1: Write failing deployment policy tests**

Create ignored `tests/test_repository.py` with deployment-focused assertions:

```python
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


REQUIREMENTS_DEPENDENCIES = {
    "apprise",
    "beautifulsoup4",
    "filelock",
    "selenium",
    'tomli; python_version < "3.11"',
}
PYPROJECT_DEPENDENCIES = {
    "apprise",
    "beautifulsoup4",
    "filelock",
    "selenium",
    "tomli; python_version < '3.11'",
}


def test_dependency_declarations_are_unversioned() -> None:
    requirements = {
        line.strip()
        for line in Path("requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert requirements == REQUIREMENTS_DEPENDENCIES
    assert set(pyproject["project"]["dependencies"]) == PYPROJECT_DEPENDENCIES
    assert pyproject["project"]["requires-python"] == ">=3.10"
    assert pyproject["tool"]["uv"]["package"] is False
    for declaration in requirements | set(pyproject["project"]["dependencies"]):
        package_name = declaration.split(";", 1)[0].strip()
        assert not any(token in package_name for token in ("==", ">", "<", "~=", "!="))


def test_compose_schedule_is_not_in_application_config() -> None:
    config = Path("config.toml").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "CRON_SCHEDULE" not in config
    assert "CRON_SCHEDULE" in compose
```

- [ ] **Step 2: Run deployment policy tests and verify RED**

Run:

```bash
uv run pytest tests/test_repository.py -v
```

Expected: FAIL because Compose and container files do not exist.

- [ ] **Step 3: Add the secret and schedule environment template**

Create `.env.example`:

```dotenv
CRON_SCHEDULE='0 */6 * * *'
TZ='UTC'
PRIMARY_SCHOLARONE_USERNAME='replace-me'
PRIMARY_SCHOLARONE_PASSWORD='replace-me'
PRIMARY_APPRISE_URL='replace-me'
```

Keep `.env` ignored. Single quotes prevent Compose from interpolating `$` inside example credentials. `config.toml` references the three `PRIMARY_*` secret variables and contains no schedule.

- [ ] **Step 4: Build the Python 3.14, uv-locked, multi-architecture image**

Create `.dockerignore`:

```dockerignore
.git
.github
.venv
data
demo.html
docs
tests
.env
__pycache__
.pytest_cache
.ruff_cache
README.md
README_zh.md
```

Create `Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1.7
FROM python:3.14-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa \
    /uv /uvx /bin/

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.47

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/app \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-driver \
        curl \
        tzdata; \
    case "${TARGETARCH}" in \
        amd64) checksum="dcb1403c188a9438c47d4bba82a9c357fc9351ce91627fb2bae627f0f5becfc4" ;; \
        arm64) checksum="e1124aa34294e2bb8ab7002f347f4363ba35097f3daf4d3c44e9d813c1fb2bb8" ;; \
        *) echo "Unsupported container architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL \
        -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${checksum}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

COPY main.py config.toml ./

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app \
    && mkdir -p /app/data \
    && chown -R app:app /app /home/app

USER app

CMD ["/app/.venv/bin/python", "/app/main.py", "--config", "/app/config.toml"]
```

The Supercronic SHA-256 values are for official release `v0.2.47` on `linux/amd64` and `linux/arm64`. Debian's `chromium-driver` exact package dependency keeps Chromium and driver versions aligned.

- [ ] **Step 5: Add immediate execution followed by validated cron scheduling**

Create `docker-compose.yml`:

```yaml
services:
  checker:
    build:
      context: .
    init: true
    restart: unless-stopped
    env_file:
      - .env
    environment:
      CHROME_BIN: /usr/bin/chromium
      CHROMEDRIVER_PATH: /usr/bin/chromedriver
      RUNNING_IN_DOCKER: "1"
      CRON_SCHEDULE: ${CRON_SCHEDULE:?Set CRON_SCHEDULE in .env}
      TZ: ${TZ:-UTC}
    volumes:
      - ./config.toml:/app/config.toml:ro
      - submission-data:/app/data
    shm_size: "1gb"
    command:
      - /bin/sh
      - -euc
      - |
        printf '%s %s\n' \
          "$${CRON_SCHEDULE}" \
          '/app/.venv/bin/python /app/main.py --config /app/config.toml' \
          > /tmp/check-submission-status.crontab
        supercronic -test /tmp/check-submission-status.crontab
        /app/.venv/bin/python /app/main.py --config /app/config.toml \
          || printf '%s\n' 'Initial check failed; scheduled checks will continue.' >&2
        exec supercronic -split-logs /tmp/check-submission-status.crontab

volumes:
  submission-data:
```

The doubled `$$` deliberately defers expansion to the container. Supercronic inherits `.env`, validates syntax, logs job output, handles signals, and serializes scheduled executions. The application lock still protects against manual overlap.

- [ ] **Step 6: Add test-code-free native and Docker smoke CI**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  native-smoke:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.10", "3.14"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
      - uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0
        with:
          version: "0.11.28"
          python-version: ${{ matrix.python-version }}
          enable-cache: true
      - run: uv sync --locked
      - run: uv run --locked python -m py_compile main.py
      - run: uv run --locked python -c "import main"
      - run: uv run --locked python main.py --help
      - run: uv run --locked ruff check main.py
      - run: uv run --locked ruff format --check main.py

  repository-policy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
      - uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0
        with:
          version: "0.11.28"
          python-version: "3.14"
      - run: uv sync --locked
      - name: Verify tracked Python and language policy
        run: >-
          uv run --locked python -c "import pathlib,re,subprocess,sys;
          files=subprocess.check_output(['git','ls-files'],text=True).splitlines();
          py=[item for item in files if item.endswith('.py')];
          han=re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]');
          bad=[item for item in files if item != 'README_zh.md'
          and han.search(pathlib.Path(item).read_text(encoding='utf-8'))];
          print({'python':py,'han_violations':bad});
          sys.exit(0 if py == ['main.py'] and not bad else 1)"

  docker-smoke:
    strategy:
      fail-fast: false
      matrix:
        include:
          - runner: ubuntu-24.04
            platform: linux/amd64
          - runner: ubuntu-24.04-arm
            platform: linux/arm64
    runs-on: ${{ matrix.runner }}
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
      - run: cp .env.example .env
      - run: docker compose config --quiet
      - run: docker build --platform "${{ matrix.platform }}" --tag check-submission-status:ci .
      - run: >-
          docker run --rm --entrypoint /bin/sh check-submission-status:ci
          -c 'test "$(id -u)" -ne 0 && chromium --version && chromedriver --version
          && python -c "import apprise, bs4, filelock, selenium"'
```

The workflow tracks smoke commands only, not unit-test code or HTML fixtures.

- [ ] **Step 7: Validate the deployment and verify GREEN**

Ensure a local `.env` exists without overwriting user secrets, then run:

```bash
cp -n .env.example .env
uv run pytest tests/test_repository.py -v
docker compose config --quiet
docker build --tag check-submission-status:local .
docker run --rm --entrypoint /bin/sh check-submission-status:local \
  -c 'test "$(id -u)" -ne 0 && chromium --version && chromedriver --version \
  && python -c "import apprise, bs4, filelock, selenium"'
```

Expected: policy tests pass, Compose parses without printing resolved secrets, the native-architecture image builds, it runs as non-root, and all browser/Python dependencies import.

- [ ] **Step 8: Commit deployment and smoke automation**

```bash
git add .dockerignore .env.example .github/workflows/ci.yml Dockerfile docker-compose.yml
git diff --cached --check
git commit -m "build: add scheduled Docker Compose deployment"
```

---

### Task 10: English-Only Tracked Tree, Complete Bilingual Guides, and Final Acceptance

**Files:**
- Replace: `README.md`
- Create: `README_zh.md`
- Modify: `.gitignore`
- Delete: `configure.sh`
- Modify if verification requires: `main.py`, `config.toml`, container/metadata files
- Test locally only: `tests/test_repository.py`

**Interfaces:**
- Consumes: every implemented command, config field, state rule, and deployment path.
- Produces: complete operator documentation for native Linux/macOS/Windows and Docker Compose.

- [ ] **Step 1: Extend repository acceptance tests and verify RED**

Add ignored assertions:

```python
import re
import subprocess


HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")


def tracked_files() -> list[str]:
    return subprocess.check_output(["git", "ls-files"], text=True).splitlines()


def test_only_main_is_tracked_python_and_only_chinese_readme_has_han() -> None:
    files = tracked_files()
    assert [path for path in files if path.endswith(".py")] == ["main.py"]
    violations = [
        path
        for path in files
        if path != "README_zh.md"
        and HAN.search(Path(path).read_text(encoding="utf-8"))
    ]
    assert violations == []


def test_legacy_runtime_and_mail_implementation_are_gone() -> None:
    assert not Path("configure.sh").exists()
    assert not Path("config.example.py").exists()
    assert not Path("log.py").exists()
    source = Path("main.py").read_text(encoding="utf-8")
    assert "smtplib" not in source
    assert "splinter" not in source
    assert "from config import" not in source


def test_local_test_material_is_ignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "demo.html", "tests/test_repository.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == ["demo.html", "tests/test_repository.py"]
```

Also add this concrete history/security test to `tests/test_database.py`; it populates checks,
observations, a notification batch, and a delivery before changing configuration:

```python
BUSINESS_TABLES = tuple(sorted(EXPECTED_TABLES))


def database_text_values(conn: sqlite3.Connection) -> list[str]:
    values: list[str] = []
    for table in BUSINESS_TABLES:
        columns = [
            row["name"]
            for row in conn.execute(f'PRAGMA table_info("{table}")')
            if "TEXT" in row["type"].upper()
        ]
        if not columns:
            continue
        select_list = ", ".join(f'"{column}"' for column in columns)
        for row in conn.execute(f'SELECT {select_list} FROM "{table}"'):
            values.extend(value for value in row if isinstance(value, str))
    return values


def test_database_omits_credentials_and_preserves_history(tmp_path: Path) -> None:
    password = "database-password-sentinel"
    destination = "json://token-sentinel@example.invalid/path"
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    configured = main.AccountConfig(
        name="primary",
        url="https://mc.manuscriptcentral.com/example",
        username="author@example.com",
        password=password,
        manuscript_ids=("AP2606-0001", "AP2606-0002"),
        apprise_urls=(destination,),
    )
    conn = main.connect_database(tmp_path / "submissions.db")
    main.migrate_database(conn)
    reconciled = main.reconcile_configuration(conn, [configured], now)["primary"]
    check_id = main.start_check(conn, reconciled.configuration_id, now)
    parsed = (
        main.ManuscriptSnapshot(
            external_id="AP2606-0001",
            title="Complete Title",
            submitted_text="09-Jul-2026",
            submitted_date=main.date(2026, 7, 9),
            status="Under Review",
        ),
    )
    prepared = main.prepare_check(conn, reconciled, check_id, parsed, now)
    assert prepared.batch is not None
    main.record_delivery(
        conn,
        prepared.batch.id,
        main.DeliveryResult(0, "json", now, True),
    )
    main.commit_prepared_check(conn, prepared, now)
    counts_before = {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in BUSINESS_TABLES
    }

    filtered = main.AccountConfig(
        name=configured.name,
        url=configured.url,
        username=configured.username,
        password=configured.password,
        manuscript_ids=("AP2606-0002",),
        apprise_urls=configured.apprise_urls,
    )
    main.reconcile_configuration(conn, [filtered], now.replace(hour=1))
    main.reconcile_configuration(conn, [], now.replace(hour=2))

    counts_after = {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in BUSINESS_TABLES
    }
    assert all(counts_after[table] >= count for table, count in counts_before.items())
    persisted_text = "\n".join(database_text_values(conn))
    assert password not in persisted_text
    assert destination not in persisted_text
    assert "token-sentinel" not in persisted_text
```

Run:

```bash
uv run pytest tests/test_repository.py -v
```

Expected: FAIL while the old Chinese README and Linux installer remain.

- [ ] **Step 2: Write the canonical English README**

Replace `README.md` with concise but complete sections in this order:

1. Project purpose and monitored fields.
2. Behavior: initial/current, new, status changed, disappeared, reappeared, per-account aggregation, any-success commit.
3. Requirements: preferred Python 3.14, floor 3.10, Chrome/Chromium, Selenium Manager network note.
4. Configuration: every `[storage]`, `[browser]`, and `[[accounts]]` key, stable names, all/filter semantics, per-account Apprise URLs, `${ENV_VAR}` expansion, and no schedule.
5. pip installation and one-shot command.
6. uv installation, `uv sync --locked`, and one-shot command.
7. Native scheduling examples for Linux cron/systemd, macOS launchd, and Windows Task Scheduler; all invoke one-shot `main.py`.
8. Docker Compose: copy `.env.example`, set secrets/schedule/timezone, `up --build -d`, immediate check, logs, stop/restart, and `config --quiet` warning.
9. SQLite persistence: named-volume location, stop-before-copy backup, restore, configuration-history behavior, and no stored credentials.
10. Troubleshooting: login selectors, missing table, driver overrides, notification retry, lock conflict, and exit codes.
11. Security and link to `README_zh.md`.

Use official links for Apprise URL syntax, Selenium Manager, uv, Docker Compose, and platform schedulers. Do not include live credentials or full URLs copied from local configuration.

- [ ] **Step 3: Write the Chinese counterpart as the only Chinese tracked file**

Create `README_zh.md` with the same headings, commands, configuration keys, behavior, warnings, backup steps, and links as `README.md`. Add reciprocal language links at the top of both files. Chinese text and punctuation must not be copied into any other tracked file.

- [ ] **Step 4: Remove the Linux-only installer and finish English cleanup**

Delete `configure.sh`. Scan code, comments, configuration, workflow, Docker files, metadata, design spec, and implementation plan. Translate or remove every remaining Chinese string outside `README_zh.md`. Keep `LICENSE` unchanged.

- [ ] **Step 5: Run complete functional and repository verification**

Run in this order:

```bash
uv lock --check
uv sync --locked
uv run pytest -v
uv run ruff check main.py tests
uv run ruff format --check main.py tests
uv run python -m py_compile main.py
uv run python main.py --help
uv run --python 3.10 --frozen --isolated python -c "import main"
uv run --python 3.14 --frozen --isolated python -c "import main"
docker compose config --quiet
docker build --tag check-submission-status:final .
git diff --check
```

Expected: every command succeeds. The smoke imports prove the unversioned latest locked dependency set imports at the Python floor and preferred version.

- [ ] **Step 6: Run tracked-language, tracked-Python, secret, and history checks**

Run:

```bash
git ls-files '*.py'
git status --short --ignored
uv run pytest tests/test_repository.py -v
```

Expected: tracked Python output is exactly `main.py`; `demo.html`, `tests/`, `.env`, and runtime data are ignored; no ignored file is staged.

The local database security test above searches every business-table text column after a complete
successful notification path. Its before/after row counts cover both filter and account removal.
The notification `caplog` test separately proves that arbitrary exception and third-party log text
cannot expose full destinations or decoded credential fragments.

- [ ] **Step 7: Commit final documentation and cleanup**

```bash
git add README.md README_zh.md .gitignore configure.sh main.py config.toml
git diff --cached --check
git diff --cached --name-only
git commit -m "docs: add cross-platform operation guides"
```

Expected: no path under `tests/`, no `demo.html`, no `.env`, and no database file is staged.

- [ ] **Step 8: Inspect the final commit range**

```bash
git status --short
git log --oneline 225c989..HEAD
git diff --stat 225c989..HEAD
```

Expected: the working tree has no visible untracked test material because it is ignored, the implementation commits are focused, and the final diff matches the approved design without unrelated changes.

## Plan Completion Gate

Before declaring implementation complete, rerun the Task 10 verification from a clean process, inspect actual command output, and confirm all of these statements with evidence:

- The ignored tests were observed failing before each production behavior was written.
- The ignored tests and `demo.html` never entered a commit.
- Only `main.py` is tracked as Python source.
- Only `README_zh.md` contains Han characters in the tracked tree.
- `requirements.txt` and `pyproject.toml` direct dependencies remain unversioned.
- `uv.lock` resolves on Python 3.10 and 3.14.
- Docker builds on the local architecture and CI covers `amd64` and `arm64`.
- Compose validates its crontab, checks immediately, and keeps scheduling after a transient initial failure.
- A real local SQLite run proves initial, retry, disappearance, reappearance, configuration-removal, and history-preservation behavior.
- No password or user-supplied, real, or credential-bearing Apprise destination appears in SQLite,
  logs, tracked files, or test output.

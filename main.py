import argparse
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
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
from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ruff: noqa: F401


LOGGER = logging.getLogger(__name__)

ENVIRONMENT_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

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
ACCOUNT_KEYS = frozenset({"name", "url", "username", "password", "manuscript_ids", "apprise_urls"})

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

SCHEMA_VERSION = 1

SCHEMA_V1 = """
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
"""

_MISSING = object()


class ConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Configuration is invalid:\n- " + "\n- ".join(self.errors))


class DashboardParseError(ValueError):
    pass


class DatabaseError(RuntimeError):
    pass


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
class ReconciledAccount:
    account_id: int
    configuration_id: int
    config: AccountConfig
    target_ids: frozenset[str]
    needs_initial_notification: bool


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


def calculate_events(
    current: Mapping[str, ManuscriptSnapshot],
    accepted: Mapping[str, AcceptedState],
    *,
    initial_verification: bool,
) -> tuple[ManuscriptEvent, ...]:
    events: list[ManuscriptEvent] = []
    for external_id in sorted(set(current) | set(accepted)):
        current_snapshot = current.get(external_id)
        previous = accepted.get(external_id)
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
        events.append(
            ManuscriptEvent(
                kind=kind,
                current=current_snapshot,
                previous=None if previous is None else previous.snapshot,
            )
        )
    return tuple(events)


def _notification_event_lines(event: ManuscriptEvent) -> list[str]:
    snapshot = event.current or event.previous
    if snapshot is None:
        raise RuntimeError("A manuscript event requires a snapshot.")
    lines = [
        f"Event: {event.kind.value}",
        f"ID: {event.external_id}",
        f"Title: {snapshot.title}",
        f"Submitted: {snapshot.submitted_text}",
    ]
    if event.kind is EventType.STATUS_CHANGED:
        if event.previous is None or event.current is None:
            raise RuntimeError("A status change requires previous and current snapshots.")
        lines.extend(
            (
                f"Previous status: {event.previous.status}",
                f"Current status: {event.current.status}",
            )
        )
    elif event.kind is EventType.DISAPPEARED:
        lines.append(f"Last known status: {snapshot.status}")
    else:
        lines.append(f"Status: {snapshot.status}")
    return lines


def build_notification(
    account_name: str,
    checked_at: datetime,
    events: Sequence[ManuscriptEvent],
    *,
    initial_verification: bool,
) -> NotificationMessage | None:
    if not events and not initial_verification:
        return None

    reason = "initial_verification" if initial_verification else "manuscript_changes"
    title = (
        f"Submission status verification for {account_name}"
        if initial_verification
        else f"Submission status changes for {account_name}"
    )
    sections = [f"Account: {account_name}\nChecked at: {utc_text(checked_at)}"]
    if events:
        sections.extend(
            "\n".join(_notification_event_lines(event))
            for event in sorted(events, key=lambda event: event.external_id)
        )
    else:
        sections.append("No manuscripts were found in the current scope.")
    return NotificationMessage(
        reason=reason,
        title=title,
        body="\n\n".join(sections),
        event_count=len(events),
    )


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required.")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def recover_interrupted_checks(conn: sqlite3.Connection, recovered_at: datetime) -> int:
    with conn:
        cursor = conn.execute(
            """
            UPDATE checks
            SET completed_at = ?,
                outcome = 'failed',
                error_type = 'InterruptedRun',
                error_message = 'The previous check was interrupted before completion.'
            WHERE outcome = 'running'
            """,
            (utc_text(recovered_at),),
        )
    return cursor.rowcount


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
        "SELECT id FROM tracking_periods WHERE manuscript_id = ? AND ended_at IS NULL",
        (manuscript_id,),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO tracking_periods(manuscript_id, started_at, started_reason) VALUES (?, ?, ?)",
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


def reconcile_configuration(
    conn: sqlite3.Connection,
    accounts: Sequence[AccountConfig],
    now: datetime,
) -> dict[str, ReconciledAccount]:
    reconciled: dict[str, ReconciledAccount] = {}
    configured_names = {account.name for account in accounts}
    changed_at = utc_text(now)

    with conn:
        account_rows = conn.execute(
            "SELECT id, name, active, needs_initial_notification FROM accounts"
        ).fetchall()
        for row in account_rows:
            if row["name"] in configured_names:
                continue
            account_id = int(row["id"])
            if bool(row["active"]) or bool(row["needs_initial_notification"]):
                conn.execute(
                    "UPDATE accounts SET active = ?, needs_initial_notification = ?, "
                    "updated_at = ? WHERE id = ?",
                    (0, 0, changed_at, account_id),
                )
            conn.execute(
                "UPDATE account_configurations SET active_until = ? "
                "WHERE account_id = ? AND active_until IS NULL",
                (changed_at, account_id),
            )
            _close_active_periods(conn, account_id, changed_at, "account_removal")

        for config in accounts:
            target_ids = frozenset(config.manuscript_ids)
            account_row = conn.execute(
                "SELECT id, active, needs_initial_notification FROM accounts WHERE name = ?",
                (config.name,),
            ).fetchone()
            is_new = account_row is None
            is_reactivated = account_row is not None and not bool(account_row["active"])
            if account_row is None:
                cursor = conn.execute(
                    "INSERT INTO accounts(name, active, needs_initial_notification, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?)",
                    (config.name, 1, 1, changed_at, changed_at),
                )
                account_id = int(cursor.lastrowid)
                needs_initial_notification = True
            else:
                account_id = int(account_row["id"])
                needs_initial_notification = bool(account_row["needs_initial_notification"])
                if is_reactivated:
                    needs_initial_notification = True
                    conn.execute(
                        "UPDATE accounts SET active = ?, needs_initial_notification = ?, "
                        "updated_at = ? WHERE id = ?",
                        (1, 1, changed_at, account_id),
                    )

            active_configuration = conn.execute(
                "SELECT id, url, username, track_all FROM account_configurations "
                "WHERE account_id = ? AND active_until IS NULL",
                (account_id,),
            ).fetchone()
            configuration_id: int | None = None
            if active_configuration is not None:
                configuration_id = int(active_configuration["id"])
                stored_targets = frozenset(
                    row["external_id"]
                    for row in conn.execute(
                        "SELECT external_id FROM account_configuration_targets "
                        "WHERE configuration_id = ?",
                        (configuration_id,),
                    ).fetchall()
                )
                identical = (
                    active_configuration["url"] == config.url
                    and active_configuration["username"] == config.username
                    and bool(active_configuration["track_all"]) == config.track_all
                    and stored_targets == target_ids
                )
                if not identical:
                    conn.execute(
                        "UPDATE account_configurations SET active_until = ? WHERE id = ?",
                        (changed_at, configuration_id),
                    )
                    configuration_id = None

            if configuration_id is None:
                cursor = conn.execute(
                    "INSERT INTO account_configurations(account_id, url, username, track_all, "
                    "active_from) VALUES (?, ?, ?, ?, ?)",
                    (account_id, config.url, config.username, int(config.track_all), changed_at),
                )
                configuration_id = int(cursor.lastrowid)
                for external_id in sorted(target_ids):
                    conn.execute(
                        "INSERT INTO account_configuration_targets(configuration_id, external_id) "
                        "VALUES (?, ?)",
                        (configuration_id, external_id),
                    )
                if not is_new and not is_reactivated:
                    conn.execute(
                        "UPDATE accounts SET updated_at = ? WHERE id = ?",
                        (changed_at, account_id),
                    )

            if not config.track_all:
                _close_active_periods(
                    conn,
                    account_id,
                    changed_at,
                    "filter_removal",
                    keep_external_ids=target_ids,
                )
                if is_new:
                    period_reason = "account_activation"
                elif is_reactivated:
                    period_reason = "scope_reactivation"
                else:
                    period_reason = "filter_addition"
                for external_id in sorted(target_ids):
                    manuscript_id = _get_or_create_manuscript(
                        conn,
                        account_id,
                        external_id,
                        changed_at,
                    )
                    _open_tracking_period(
                        conn,
                        manuscript_id,
                        changed_at,
                        period_reason,
                    )

            reconciled[config.name] = ReconciledAccount(
                account_id=account_id,
                configuration_id=configuration_id,
                config=config,
                target_ids=target_ids,
                needs_initial_notification=needs_initial_notification,
            )

    return reconciled


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


def _accepted_snapshot(row: sqlite3.Row) -> ManuscriptSnapshot | None:
    if not bool(row["accepted_present"]):
        return None
    return ManuscriptSnapshot(
        external_id=row["external_id"],
        title=row["accepted_title"],
        submitted_text=row["accepted_submitted_text"],
        submitted_date=date.fromisoformat(row["accepted_submitted_date"]),
        status=row["accepted_status"],
    )


def _upsert_current_state(
    conn: sqlite3.Connection,
    update: ProjectionUpdate,
    updated_at: str,
) -> None:
    cursor = conn.execute(
        "INSERT INTO current_states(tracking_period_id, observation_id, updated_at) "
        "VALUES (?, ?, ?) ON CONFLICT(tracking_period_id) DO UPDATE SET "
        "observation_id = excluded.observation_id, updated_at = excluded.updated_at",
        (update.tracking_period_id, update.observation_id, updated_at),
    )
    if cursor.rowcount != 1:
        raise DatabaseError("The current state could not be updated.")


def prepare_check(
    conn: sqlite3.Connection,
    account: ReconciledAccount,
    check_id: int,
    parsed: Sequence[ManuscriptSnapshot],
    observed_at: datetime,
) -> PreparedCheck:
    observed_text = utc_text(observed_at)
    parsed_count = len(parsed)
    parsed_by_id = {snapshot.external_id: snapshot for snapshot in parsed}

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        check_row = conn.execute(
            "SELECT checks.configuration_id, checks.outcome, "
            "account_configurations.account_id, account_configurations.track_all, "
            "EXISTS(SELECT 1 FROM observations WHERE observations.check_id = checks.id) "
            "AS has_observations, "
            "EXISTS(SELECT 1 FROM notification_batches "
            "WHERE notification_batches.check_id = checks.id) AS has_batch "
            "FROM checks JOIN account_configurations "
            "ON account_configurations.id = checks.configuration_id WHERE checks.id = ?",
            (check_id,),
        ).fetchone()
        if (
            check_row is None
            or check_row["outcome"] != "running"
            or int(check_row["configuration_id"]) != account.configuration_id
            or int(check_row["account_id"]) != account.account_id
            or bool(check_row["track_all"]) != account.config.track_all
        ):
            raise DatabaseError("The running check does not belong to the reconciled account.")
        if bool(check_row["has_observations"]) or bool(check_row["has_batch"]):
            raise DatabaseError("The running check has already been prepared.")

        if account.config.track_all:
            current = parsed_by_id
            for external_id in sorted(current):
                manuscript_id = _get_or_create_manuscript(
                    conn,
                    account.account_id,
                    external_id,
                    observed_text,
                )
                _open_tracking_period(
                    conn,
                    manuscript_id,
                    observed_text,
                    "track_all_discovery",
                )
        else:
            current = {
                external_id: snapshot
                for external_id, snapshot in parsed_by_id.items()
                if external_id in account.target_ids
            }

        period_rows = conn.execute(
            "SELECT tracking_periods.id AS tracking_period_id, "
            "manuscripts.id AS manuscript_id, manuscripts.external_id, "
            "current_states.observation_id AS accepted_observation_id, "
            "accepted_observations.present AS accepted_present, "
            "accepted_observations.title AS accepted_title, "
            "accepted_observations.submitted_text AS accepted_submitted_text, "
            "accepted_observations.submitted_date AS accepted_submitted_date, "
            "accepted_observations.status AS accepted_status "
            "FROM tracking_periods "
            "JOIN manuscripts ON manuscripts.id = tracking_periods.manuscript_id "
            "LEFT JOIN current_states "
            "ON current_states.tracking_period_id = tracking_periods.id "
            "LEFT JOIN observations AS accepted_observations "
            "ON accepted_observations.id = current_states.observation_id "
            "WHERE manuscripts.account_id = ? AND tracking_periods.ended_at IS NULL "
            "ORDER BY manuscripts.external_id",
            (account.account_id,),
        ).fetchall()

        accepted: dict[str, AcceptedState] = {}
        for row in period_rows:
            if row["accepted_observation_id"] is None:
                continue
            accepted[row["external_id"]] = AcceptedState(
                tracking_period_id=int(row["tracking_period_id"]),
                observation_id=int(row["accepted_observation_id"]),
                present=bool(row["accepted_present"]),
                snapshot=_accepted_snapshot(row),
            )

        observation_updates: dict[str, ProjectionUpdate] = {}
        for row in period_rows:
            external_id = row["external_id"]
            snapshot = current.get(external_id)
            if snapshot is None:
                values: tuple[object, ...] = (0, None, None, None, None)
            else:
                values = (
                    1,
                    snapshot.title,
                    snapshot.submitted_text,
                    snapshot.submitted_date.isoformat(),
                    snapshot.status,
                )
                cursor = conn.execute(
                    "UPDATE manuscripts SET first_seen_at = COALESCE(first_seen_at, ?) "
                    "WHERE id = ?",
                    (observed_text, row["manuscript_id"]),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError("The observed manuscript was not found.")
            cursor = conn.execute(
                "INSERT INTO observations(check_id, manuscript_id, tracking_period_id, "
                "present, title, submitted_text, submitted_date, status, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    check_id,
                    row["manuscript_id"],
                    row["tracking_period_id"],
                    *values,
                    observed_text,
                ),
            )
            if cursor.rowcount != 1:
                raise DatabaseError("The manuscript observation could not be stored.")
            observation_updates[external_id] = ProjectionUpdate(
                tracking_period_id=int(row["tracking_period_id"]),
                observation_id=int(cursor.lastrowid),
            )

        events = calculate_events(
            current,
            accepted,
            initial_verification=account.needs_initial_notification,
        )
        event_ids = {event.external_id for event in events}
        deferred_updates: list[ProjectionUpdate] = []
        for external_id, update in observation_updates.items():
            if external_id in event_ids:
                deferred_updates.append(update)
            elif external_id in accepted or external_id in current:
                _upsert_current_state(conn, update, observed_text)

        message = build_notification(
            account.config.name,
            observed_at,
            events,
            initial_verification=account.needs_initial_notification,
        )
        batch: StoredNotificationBatch | None = None
        if message is not None:
            cursor = conn.execute(
                "INSERT INTO notification_batches(check_id, account_id, reason, title, body, "
                "event_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    check_id,
                    account.account_id,
                    message.reason,
                    message.title,
                    message.body,
                    message.event_count,
                    observed_text,
                ),
            )
            if cursor.rowcount != 1:
                raise DatabaseError("The notification batch could not be stored.")
            batch = StoredNotificationBatch(
                id=int(cursor.lastrowid),
                title=message.title,
                body=message.body,
                event_count=message.event_count,
                reason=message.reason,
            )

    return PreparedCheck(
        check_id=check_id,
        account_id=account.account_id,
        configuration_id=account.configuration_id,
        parsed_count=parsed_count,
        events=events,
        batch=batch,
        deferred_updates=tuple(deferred_updates),
        clear_initial_on_commit=account.needs_initial_notification,
    )


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
            "WHERE id = ? AND configuration_id = ? AND outcome = 'running' "
            "AND NOT EXISTS (SELECT 1 FROM notification_batches "
            "WHERE notification_batches.check_id = checks.id)",
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


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def normalize_manuscript_id(value: str) -> str:
    return normalize_whitespace(value.split("(", 1)[0])


def parse_submitted_date(value: str) -> date:
    parts = normalize_whitespace(value).split("-")
    if len(parts) != 3 or parts[1] not in ENGLISH_MONTHS:
        raise DashboardParseError("Submission date must use DD-Mon-YYYY.")
    try:
        return date(int(parts[2]), ENGLISH_MONTHS[parts[1]], int(parts[0]))
    except ValueError as exc:
        raise DashboardParseError("Submission date is invalid.") from exc


def _dashboard_rows(table: Tag) -> list[Tag]:
    tbody = table.find("tbody", recursive=False)
    if isinstance(tbody, Tag):
        return [row for row in tbody.find_all("tr", recursive=False) if isinstance(row, Tag)]

    direct_rows = [row for row in table.find_all("tr", recursive=False) if isinstance(row, Tag)]
    has_manuscript_row = any(
        row.find("td", attrs={"data-label": True}, recursive=False) is not None
        for row in direct_rows
    )
    if not has_manuscript_row:
        raise DashboardParseError("Dashboard row structure is missing.")
    return direct_rows


def _dashboard_cell(row: Tag, label: str, row_number: int) -> Tag:
    cell = row.find("td", attrs={"data-label": label}, recursive=False)
    if not isinstance(cell, Tag):
        raise DashboardParseError(f"Dashboard row {row_number} is missing the {label} cell.")
    return cell


def _required_dashboard_text(value: str, field: str, row_number: int) -> str:
    normalized = normalize_whitespace(value)
    if not normalized:
        raise DashboardParseError(f"Dashboard row {row_number} has an empty {field} field.")
    return normalized


def parse_dashboard(html: str) -> tuple[ManuscriptSnapshot, ...]:
    document = BeautifulSoup(html, "html.parser")
    table = document.find("table", id="authorDashboardQueue")
    if not isinstance(table, Tag):
        raise DashboardParseError("Dashboard table #authorDashboardQueue is missing.")

    snapshots: list[ManuscriptSnapshot] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(_dashboard_rows(table), start=1):
        status_cell = _dashboard_cell(row, "status", row_number)
        status_element = status_cell.select_one("span.pagecontents")
        if not isinstance(status_element, Tag):
            raise DashboardParseError(f"Dashboard row {row_number} is missing its status value.")
        status = _required_dashboard_text(
            status_element.get_text(" ", strip=True),
            "status",
            row_number,
        )

        id_cell = _dashboard_cell(row, "ID", row_number)
        external_id = normalize_manuscript_id(id_cell.get_text(" ", strip=True))
        if not external_id:
            raise DashboardParseError(f"Dashboard row {row_number} has an empty ID field.")
        if external_id in seen_ids:
            raise DashboardParseError(
                f"Dashboard contains duplicate manuscript ID {external_id!r}."
            )
        seen_ids.add(external_id)

        title_cell = _dashboard_cell(row, "title", row_number)
        title_document = BeautifulSoup(str(title_cell), "html.parser")
        for anchor in title_document.find_all("a"):
            anchor.decompose()
        title = _required_dashboard_text(
            title_document.get_text(" ", strip=True),
            "title",
            row_number,
        )

        submitted_cell = _dashboard_cell(row, "submitted", row_number)
        submitted_text = _required_dashboard_text(
            submitted_cell.get_text(" ", strip=True),
            "submitted",
            row_number,
        )
        try:
            submitted_date = parse_submitted_date(submitted_text)
        except DashboardParseError as exc:
            raise DashboardParseError(
                f"Dashboard row {row_number} has an invalid submission date: {exc}"
            ) from exc

        snapshots.append(
            ManuscriptSnapshot(
                external_id=external_id,
                title=title,
                submitted_text=submitted_text,
                submitted_date=submitted_date,
                status=status,
            )
        )

    return tuple(snapshots)


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


def _add_unknown_key_errors(
    value: Mapping[str, object],
    *,
    allowed: AbstractSet[str],
    path: str,
    errors: list[str],
) -> None:
    for key in value:
        if key not in allowed:
            errors.append(f"{path}.{key}: unknown key.")


def _read_mapping(
    value: object,
    *,
    path: str,
    errors: list[str],
) -> Mapping[str, object] | None:
    if value is _MISSING:
        errors.append(f"{path}: is required.")
        return None
    if not isinstance(value, Mapping):
        errors.append(f"{path}: must be a table.")
        return None
    return value


def _read_string(
    value: object,
    *,
    path: str,
    environ: Mapping[str, str],
    errors: list[str],
    strip: bool = True,
    allow_empty: bool = False,
) -> str | None:
    if value is _MISSING:
        errors.append(f"{path}: is required.")
        return None
    if not isinstance(value, str):
        errors.append(f"{path}: must be a string.")
        return None
    try:
        expanded = expand_environment(value, environ)
    except ConfigError as exc:
        errors.extend(f"{path}: {message}" for message in exc.errors)
        return None
    result = expanded.strip() if strip else expanded
    if not allow_empty and not result:
        errors.append(f"{path}: must not be empty.")
        return None
    return result


def _read_positive_integer(
    value: object,
    *,
    path: str,
    errors: list[str],
) -> int | None:
    if value is _MISSING:
        errors.append(f"{path}: is required.")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        errors.append(f"{path}: must be a positive integer.")
        return None
    return value


def _resolve_path(value: str, *, config_dir: Path, path: str, errors: list[str]) -> Path | None:
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = config_dir / candidate
        return candidate.resolve()
    except (OSError, RuntimeError):
        errors.append(f"{path}: could not be resolved.")
        return None


def _parse_storage(
    raw: object,
    *,
    config_dir: Path,
    environ: Mapping[str, str],
    errors: list[str],
) -> StorageConfig | None:
    value = _read_mapping(raw, path="storage", errors=errors)
    if value is None:
        return None
    _add_unknown_key_errors(value, allowed=STORAGE_KEYS, path="storage", errors=errors)
    database_value = _read_string(
        value.get("database_path", _MISSING),
        path="storage.database_path",
        environ=environ,
        errors=errors,
    )
    if database_value is None:
        return None
    database_path = _resolve_path(
        database_value,
        config_dir=config_dir,
        path="storage.database_path",
        errors=errors,
    )
    if database_path is None:
        return None
    return StorageConfig(database_path=database_path)


def _parse_browser(
    raw: object,
    *,
    config_dir: Path,
    environ: Mapping[str, str],
    errors: list[str],
) -> BrowserConfig | None:
    value = _read_mapping(raw, path="browser", errors=errors)
    if value is None:
        return None
    _add_unknown_key_errors(value, allowed=BROWSER_KEYS, path="browser", errors=errors)

    headless_value = value.get("headless", _MISSING)
    if not isinstance(headless_value, bool):
        errors.append("browser.headless: must be a boolean.")
        headless = None
    else:
        headless = headless_value

    element_timeout = _read_positive_integer(
        value.get("element_timeout_seconds", _MISSING),
        path="browser.element_timeout_seconds",
        errors=errors,
    )
    page_load_timeout = _read_positive_integer(
        value.get("page_load_timeout_seconds", _MISSING),
        path="browser.page_load_timeout_seconds",
        errors=errors,
    )

    optional_paths: dict[str, Path | None] = {"binary_path": None, "driver_path": None}
    for key in optional_paths:
        if key not in value:
            continue
        field_path = f"browser.{key}"
        path_value = _read_string(
            value[key],
            path=field_path,
            environ=environ,
            errors=errors,
        )
        if path_value is not None:
            optional_paths[key] = _resolve_path(
                path_value,
                config_dir=config_dir,
                path=field_path,
                errors=errors,
            )

    if headless is None or element_timeout is None or page_load_timeout is None:
        return None
    return BrowserConfig(
        headless=headless,
        element_timeout_seconds=element_timeout,
        page_load_timeout_seconds=page_load_timeout,
        binary_path=optional_paths["binary_path"],
        driver_path=optional_paths["driver_path"],
    )


def _parse_account(
    raw: object,
    *,
    index: int,
    environ: Mapping[str, str],
    seen_names: dict[str, int],
    errors: list[str],
) -> AccountConfig | None:
    account_path = f"accounts[{index}]"
    value = _read_mapping(raw, path=account_path, errors=errors)
    if value is None:
        return None
    error_count = len(errors)
    _add_unknown_key_errors(value, allowed=ACCOUNT_KEYS, path=account_path, errors=errors)

    name = _read_string(
        value.get("name", _MISSING),
        path=f"{account_path}.name",
        environ=environ,
        errors=errors,
    )
    if name is not None:
        if name in seen_names:
            errors.append(
                f"{account_path}.name: duplicates account name at position {seen_names[name]}."
            )
        else:
            seen_names[name] = index

    url = _read_string(
        value.get("url", _MISSING),
        path=f"{account_path}.url",
        environ=environ,
        errors=errors,
    )
    if url is not None:
        try:
            parts = urlsplit(url)
            valid_url = parts.scheme.lower() in {"http", "https"} and parts.hostname is not None
        except ValueError:
            valid_url = False
        if not valid_url:
            errors.append(f"{account_path}.url: must be an HTTP(S) URL.")

    username = _read_string(
        value.get("username", _MISSING),
        path=f"{account_path}.username",
        environ=environ,
        errors=errors,
    )
    password = _read_string(
        value.get("password", _MISSING),
        path=f"{account_path}.password",
        environ=environ,
        errors=errors,
        strip=False,
        allow_empty=True,
    )

    manuscript_ids: tuple[str, ...] | None = None
    manuscript_values = value.get("manuscript_ids", _MISSING)
    if manuscript_values is _MISSING:
        errors.append(f"{account_path}.manuscript_ids: is required.")
    elif not isinstance(manuscript_values, list):
        errors.append(f"{account_path}.manuscript_ids: must be an array.")
    else:
        normalized_ids: list[str] = []
        for manuscript_index, manuscript_value in enumerate(manuscript_values):
            manuscript_path = f"{account_path}.manuscript_ids[{manuscript_index}]"
            expanded_id = _read_string(
                manuscript_value,
                path=manuscript_path,
                environ=environ,
                errors=errors,
                strip=False,
                allow_empty=True,
            )
            if expanded_id is None:
                continue
            normalized_id = normalize_manuscript_id(expanded_id)
            if not normalized_id:
                errors.append(f"{manuscript_path}: must not be empty after normalization.")
                continue
            normalized_ids.append(normalized_id)
        manuscript_ids = tuple(dict.fromkeys(normalized_ids))

    apprise_urls: tuple[str, ...] | None = None
    destination_values = value.get("apprise_urls", _MISSING)
    if destination_values is _MISSING:
        errors.append(f"{account_path}.apprise_urls: is required.")
    elif not isinstance(destination_values, list):
        errors.append(f"{account_path}.apprise_urls: must be an array.")
    elif not destination_values:
        errors.append(f"{account_path}.apprise_urls: must contain at least one destination.")
    else:
        parsed_destinations: list[str] = []
        seen_destinations: dict[str, int] = {}
        for destination_index, destination_value in enumerate(destination_values):
            destination_path = f"{account_path}.apprise_urls[{destination_index}]"
            destination = _read_string(
                destination_value,
                path=destination_path,
                environ=environ,
                errors=errors,
            )
            if destination is None:
                continue
            if destination in seen_destinations:
                errors.append(
                    f"{destination_path}: duplicates destination at position "
                    f"{seen_destinations[destination]}."
                )
            else:
                seen_destinations[destination] = destination_index
            parsed_destinations.append(destination)
        apprise_urls = tuple(parsed_destinations)

    if len(errors) != error_count:
        return None
    assert name is not None
    assert url is not None
    assert username is not None
    assert password is not None
    assert manuscript_ids is not None
    assert apprise_urls is not None
    return AccountConfig(
        name=name,
        url=url,
        username=username,
        password=password,
        manuscript_ids=manuscript_ids,
        apprise_urls=apprise_urls,
    )


def parse_config(
    raw: Mapping[str, object],
    *,
    config_dir: Path,
    environ: Mapping[str, str],
) -> AppConfig:
    errors: list[str] = []
    _add_unknown_key_errors(raw, allowed=ROOT_KEYS, path="root", errors=errors)

    storage = _parse_storage(
        raw.get("storage", _MISSING),
        config_dir=config_dir,
        environ=environ,
        errors=errors,
    )
    browser = _parse_browser(
        raw.get("browser", _MISSING),
        config_dir=config_dir,
        environ=environ,
        errors=errors,
    )

    accounts: list[AccountConfig] = []
    account_values = raw.get("accounts", _MISSING)
    if account_values is _MISSING:
        errors.append("accounts: is required.")
    elif not isinstance(account_values, list):
        errors.append("accounts: must be an array of tables.")
    else:
        seen_names: dict[str, int] = {}
        for index, account_value in enumerate(account_values):
            account = _parse_account(
                account_value,
                index=index,
                environ=environ,
                seen_names=seen_names,
                errors=errors,
            )
            if account is not None:
                accounts.append(account)

    if errors:
        raise ConfigError(errors)
    assert storage is not None
    assert browser is not None
    return AppConfig(storage=storage, browser=browser, accounts=tuple(accounts))


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

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

_MISSING = object()


class ConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Configuration is invalid:\n- " + "\n- ".join(self.errors))


class DashboardParseError(ValueError):
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

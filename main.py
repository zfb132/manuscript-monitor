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

_MISSING = object()


class ConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Configuration is invalid:\n- " + "\n- ".join(self.errors))


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

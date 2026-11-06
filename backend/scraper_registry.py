from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

BACKEND_DIR = Path(__file__).resolve().parent
WEBSCRAPERS_DIR = BACKEND_DIR / "Webscrappers"
SETTINGS_PATH = BACKEND_DIR / "scraper_settings.json"

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_CONCURRENT_SCRAPERS = 3


@dataclass(frozen=True)
class ScraperDefinition:
    id: str
    name: str
    module: str
    function: str
    is_async: bool


@dataclass
class LoadedScraper:
    definition: ScraperDefinition
    callable: Callable[..., Any] | None = None
    load_error: str | None = None


SCRAPER_DEFINITIONS = (
    ScraperDefinition("alfatah", "Al-Fatah", "alfatah", "search_products", True),
    ScraperDefinition("daraz", "Daraz", "daraz", "search_products", False),
    ScraperDefinition("mega", "Mega.pk", "mega", "search_products", False),
    ScraperDefinition("naheed", "Naheed.pk", "naheed", "search_products", False),
    ScraperDefinition("priceoye", "PriceOye", "priceoye", "search_products", True),
    ScraperDefinition("shopon", "Shopon.pk", "shopon", "search_products", False),
    ScraperDefinition("telex", "Telex.pk", "telex", "search_products", False),
)

_initialized = False
_init_lock = threading.Lock()
_settings_lock = threading.Lock()
_loaded: dict[str, LoadedScraper] = {}
_enabled: dict[str, bool] = {}


def initialize_registry() -> None:
    global _initialized

    with _init_lock:
        if _initialized:
            return

        definitions = _available_definitions()
        with _settings_lock:
            _enabled.clear()
            _load_settings_locked(definitions)

        for definition in definitions:
            _loaded[definition.id] = _load_scraper(definition)

        _initialized = True


def get_scrapers() -> list[dict[str, Any]]:
    initialize_registry()

    with _settings_lock:
        enabled = dict(_enabled)

    return [
        {
            "id": scraper.definition.id,
            "name": scraper.definition.name,
            "enabled": enabled.get(scraper.definition.id, True),
            "available": scraper.callable is not None,
        }
        for scraper in _loaded.values()
    ]


def set_scraper_enabled(scraper_id: str, enabled: bool) -> dict[str, Any]:
    initialize_registry()

    if scraper_id not in _loaded:
        raise KeyError(scraper_id)

    with _settings_lock:
        previous = _enabled.get(scraper_id, True)
        _enabled[scraper_id] = enabled

        try:
            _save_settings_locked()
        except Exception:
            _enabled[scraper_id] = previous
            raise

    scraper = _loaded[scraper_id]
    return {
        "id": scraper.definition.id,
        "name": scraper.definition.name,
        "enabled": enabled,
        "available": scraper.callable is not None,
    }


async def search_scrapers(
    keyword: str,
    scraper_ids: list[str],
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    initialize_registry()

    keyword = keyword.strip()
    limit = max(1, min(limit, MAX_LIMIT))
    selected = list(dict.fromkeys(scraper_ids))

    with _settings_lock:
        enabled = dict(_enabled)

    runnable: list[LoadedScraper] = []
    results: dict[str, list[Any]] = {}
    errors: dict[str, str] = {}

    for scraper_id in selected:
        scraper = _loaded.get(scraper_id)

        if scraper is None:
            errors[scraper_id] = "Unknown scraper."
            continue

        if not enabled.get(scraper_id, True):
            errors[scraper_id] = "This scraper is disabled by the administrator."
            continue

        if scraper.callable is None:
            errors[scraper_id] = "This scraper is currently unavailable."
            continue

        runnable.append(scraper)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPERS)

    async def run_scraper(
        scraper: LoadedScraper,
    ) -> tuple[str, list[Any] | None, str | None]:
        async with semaphore:
            with _settings_lock:
                is_enabled = _enabled.get(scraper.definition.id, True)

            if not is_enabled:
                return (
                    scraper.definition.id,
                    None,
                    "This scraper is disabled by the administrator.",
                )

            try:
                products = await _execute_scraper(scraper, keyword, limit)
                return scraper.definition.id, products, None
            except Exception as exc:
                return scraper.definition.id, None, _scraper_error_message(exc)

    outcomes = await asyncio.gather(*(run_scraper(scraper) for scraper in runnable))

    for scraper_id, products, error in outcomes:
        results[scraper_id] = products or []
        if error is not None:
            errors[scraper_id] = error

    return {
        "keyword": keyword,
        "results": results,
        "errors": errors,
    }


def _available_definitions() -> list[ScraperDefinition]:
    return [
        definition
        for definition in SCRAPER_DEFINITIONS
        if (WEBSCRAPERS_DIR / f"{definition.module}.py").is_file()
    ]


def _load_scraper(definition: ScraperDefinition) -> LoadedScraper:
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))

    try:
        module = importlib.import_module(f"Webscrappers.{definition.module}")
        function = getattr(module, definition.function)
    except Exception as exc:
        return LoadedScraper(
            definition=definition,
            load_error=f"{type(exc).__name__}: {exc}",
        )

    return LoadedScraper(definition=definition, callable=function)


def _load_settings_locked(definitions: list[ScraperDefinition]) -> None:
    saved: dict[str, Any] = {}

    if SETTINGS_PATH.is_file():
        try:
            payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("enabled"), dict):
                saved = payload["enabled"]
        except (OSError, ValueError):
            saved = {}

    for definition in definitions:
        value = saved.get(definition.id)
        _enabled[definition.id] = value if isinstance(value, bool) else True


def _save_settings_locked() -> None:
    payload = {"enabled": dict(sorted(_enabled.items()))}
    temp_path = SETTINGS_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(SETTINGS_PATH)


async def _execute_scraper(
    scraper: LoadedScraper,
    keyword: str,
    limit: int,
) -> list[Any]:
    function = scraper.callable

    if function is None:
        raise RuntimeError("Scraper is unavailable.")

    if scraper.definition.is_async:
        payload = await run_in_threadpool(
            _run_async_scraper,
            function,
            keyword,
            limit,
        )
    else:
        payload = await run_in_threadpool(
            _run_sync_scraper,
            function,
            keyword,
            limit,
        )

    return _extract_products(payload)


def _run_sync_scraper(
    function: Callable[..., Any],
    keyword: str,
    limit: int,
) -> Any:
    return function(keyword=keyword, limit=limit)


def _run_async_scraper(
    function: Callable[..., Any],
    keyword: str,
    limit: int,
) -> Any:
    return asyncio.run(function(keyword=keyword, limit=limit))


def _extract_products(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        products = payload.get("products")
        return products if isinstance(products, list) else []

    if isinstance(payload, list):
        return payload

    if isinstance(payload, tuple) and payload and isinstance(payload[0], list):
        return payload[0]

    return []


def _scraper_error_message(exc: Exception) -> str:
    if not isinstance(exc, HTTPException):
        return "The scraper failed. Please try again."

    detail = exc.detail

    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:500]

    if detail is not None:
        return json.dumps(detail, default=str)[:500]

    return "The scraper failed. Please try again."

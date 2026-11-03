from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from real_search_probe import configure_search

BASE_URL = "https://www.bahn.de/"
BERLIN = ZoneInfo("Europe/Berlin")
DEFAULT_END_DATE = date(2026, 12, 31)
DEFAULT_REPORT_DIR = Path("artifacts")
MAX_PAGES_PER_DAY = 60
PAGE_ADVANCE_TIMEOUT_MS = 15000
MAX_ATTEMPTS_PER_DATE = 3
INTER_DATE_DELAY_SECONDS = 2
ACCESS_ERROR_PATTERNS = (
    "access-error",
    "automated bot",
    "browser activity appears to be similar to that of an automated bot",
    "fehler-code: 751",
    "errorcode=751",
)
MONEY_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})\s*€")
TRAIN_MODE_RE = re.compile(
    r"\b(ICE|IC|EC|RE|RB|IRE|BRB|FLX|FLIXTRAIN|SEV|BUS|RJX|RJ|NJ|S\s*\d+|U\s*\d+)\b",
    re.IGNORECASE,
)
TRAIN_NUMBER_RE = re.compile(r"\bICE\s*(\d{1,4})\b", re.IGNORECASE)
TIME_RE = re.compile(r"\b(?:[01]\d|2[0-3]):[0-5]\d\b")
MONTHS = {
    "jan": 1,
    "feb": 2,
    "mär": 3,
    "mrz": 3,
    "apr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dez": 12,
}


class AccessBlockedError(RuntimeError):
    """Raised when DB displays a specific access or automation restriction."""


@dataclass
class JourneyOffer:
    day: str
    departure_time: str
    arrival_time: str
    price: Decimal | None
    train_numbers: list[str]
    transfers: int
    route: str
    url: str | None = None
    train_types: list[str] = field(default_factory=list)
    duration: str = "N/A"
    observed_at: str = ""
    fare_confirmed: bool = False
    fare_is_from: bool = False

    def as_telegram_message(self) -> str:
        price_text = "Fare not shown" if self.price is None else f"€{self.price:.2f}"
        if self.fare_is_from:
            price_text += " (displayed starting fare)"
        return (
            f"ICE ticket below €20\n"
            f"Date: {self.day}\n"
            f"Departure: {self.departure_time} → Arrival: {self.arrival_time}\n"
            f"Trains: {', '.join(self.train_numbers) or 'N/A'}\n"
            f"Transfers: {self.transfers} | Duration: {self.duration}\n"
            f"Price: {price_text}\n"
            f"Route: {self.route}\n"
            f"Observed: {self.observed_at}\n"
            f"{self.url or BASE_URL}"
        )


def berlin_now() -> datetime:
    return datetime.now(BERLIN)


def generate_dates(today: date, end_day: date = DEFAULT_END_DATE) -> list[date]:
    if today > end_day:
        return []
    return [
        today + timedelta(days=offset) for offset in range((end_day - today).days + 1)
    ]


def parse_displayed_price(raw: str) -> tuple[Decimal, bool]:
    text = (raw or "").replace("\xa0", " ")
    lower = text.lower()
    new_price_at = max(lower.rfind("neuer preis"), lower.rfind("new price"))
    search_text = text[new_price_at:] if new_price_at >= 0 else text
    match = MONEY_RE.search(search_text)
    if match is None:
        matches = list(MONEY_RE.finditer(text))
        if not matches:
            raise ValueError(f"No displayed price found in text: {raw!r}")
        match = matches[-1]
        search_text = text
    normalized = match.group(1).replace(".", "").replace(",", ".")
    try:
        price = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid displayed price in text: {raw!r}") from exc
    prefix = search_text[: match.start()].lower()
    is_from_price = re.search(r"\b(?:ab|from)\s*$", prefix) is not None
    return price, is_from_price


def parse_price(raw: str) -> Decimal:
    return parse_displayed_price(raw)[0]


def is_qualifying_fare(
    price: Decimal | None, fare_confirmed: bool, max_price: Decimal = Decimal("20.00")
) -> bool:
    return price is not None and fare_confirmed and price < max_price


def is_ice_only_journey(text: str) -> bool:
    normalized = (text or "").replace("\xa0", " ")
    modes = [
        match.upper().replace(" ", "") for match in TRAIN_MODE_RE.findall(normalized)
    ]
    if "ICE" not in modes or any(mode != "ICE" for mode in modes):
        return False
    return not re.search(
        r"\b(bus|ersatzverkehr|schienenersatzverkehr|tram|metro)\b",
        normalized,
        re.IGNORECASE,
    )


def dedupe_key(offer: JourneyOffer) -> tuple[str, str, str, tuple[str, ...]]:
    return (
        offer.day,
        offer.departure_time,
        offer.arrival_time,
        tuple(offer.train_numbers),
    )


def detect_access_block(page) -> bool:
    url = (page.url or "").lower()
    body = (page.locator("body").inner_text() or "").lower()
    return any(pattern in url or pattern in body for pattern in ACCESS_ERROR_PATTERNS)


def _offer_from_row(
    row, travel_day: date, origin: str, destination: str, observed_at: datetime
) -> JourneyOffer | None:
    text = row.inner_text(timeout=5000).replace("\xa0", " ")
    if not is_ice_only_journey(text):
        return None

    time_values = row.locator("time[datetime]").evaluate_all(
        "elements => elements.map(element => element.getAttribute('datetime'))"
    )
    time_values = [value for value in time_values if value and TIME_RE.fullmatch(value)]
    if len(time_values) < 2:
        time_values = [match.group(0) for match in TIME_RE.finditer(text)]
    departure = time_values[0] if time_values else "N/A"
    arrival = time_values[1] if len(time_values) > 1 else "N/A"

    train_numbers = list(
        dict.fromkeys(f"ICE {number}" for number in TRAIN_NUMBER_RE.findall(text))
    )
    if not train_numbers:
        return None
    transfers_match = re.search(r"(\d+)\s+Umstieg", text, re.IGNORECASE)
    transfers = (
        int(transfers_match.group(1))
        if transfers_match
        else max(0, len(train_numbers) - 1)
    )
    duration_locator = row.locator(".test-duration")
    duration = (
        duration_locator.first.inner_text().strip()
        if duration_locator.count()
        else "N/A"
    )
    try:
        price, is_from_price = parse_displayed_price(text)
    except ValueError:
        price, is_from_price = None, False

    return JourneyOffer(
        day=travel_day.isoformat(),
        departure_time=departure,
        arrival_time=arrival,
        price=price,
        train_numbers=train_numbers,
        transfers=transfers,
        route=f"{origin} → {destination}",
        train_types=["ICE"],
        duration=duration,
        observed_at=observed_at.isoformat(timespec="seconds"),
        fare_confirmed=price is not None and not is_from_price,
        fare_is_from=is_from_price,
    )


def _parse_heading_date(row) -> date | None:
    heading = row.locator(".reiseloesung-heading")
    if not heading.count():
        return None
    text = heading.first.inner_text().replace("\xa0", " ").strip()
    match = re.search(r"\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]{3,})\.?\s*(\d{4})\b", text)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower()[:3])
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _date_record(
    day: date, status: str = "NOT_AVAILABLE", reason: str | None = None
) -> dict:
    return {
        "date": day.isoformat(),
        "status": status,
        "searched_independently": False,
        "full_day_coverage_verified": False,
        "pages_inspected": 0,
        "journeys_inspected": 0,
        "lowest_observed_fare": None,
        "qualifying_journeys": [],
        "journeys": [],
        "errors": [reason] if reason else [],
        "limitations": [],
    }


def _serialize_offer(offer: JourneyOffer) -> dict:
    value = asdict(offer)
    value["price"] = str(offer.price) if offer.price is not None else None
    return value


def _scan_page_for_day(
    page, travel_day: date, origin: str, destination: str, now: datetime
):
    rows = page.locator("li.verbindung-list__result-item")
    seen: dict[tuple[str, str, str, tuple[str, ...]], JourneyOffer] = {}
    active_day = travel_day
    next_day_seen = False
    for index in range(rows.count()):
        row = rows.nth(index)
        heading_day = _parse_heading_date(row)
        if heading_day is not None:
            active_day = heading_day
            if heading_day > travel_day:
                next_day_seen = True
                break
            if heading_day < travel_day:
                continue
        if active_day != travel_day:
            continue
        offer = _offer_from_row(row, travel_day, origin, destination, berlin_now())
        if offer is None:
            continue
        if (
            travel_day == now.date()
            and offer.departure_time != "N/A"
            and offer.departure_time <= now.strftime("%H:%M")
        ):
            continue
        seen.setdefault(dedupe_key(offer), offer)
    return seen, next_day_seen


def _save_failure_artifacts(page, travel_day: date, diagnostics_dir: Path) -> None:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stem = f"failure-{travel_day.isoformat()}-{int(time.time())}"
    try:
        page.screenshot(path=str(diagnostics_dir / f"{stem}.png"), full_page=True)
        (diagnostics_dir / f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except (OSError, PlaywrightError) as exc:
        print(f"Could not save diagnostics for {travel_day}: {exc}", file=sys.stderr)


def _search_one_date(
    context,
    travel_day: date,
    *,
    origin: str,
    destination: str,
    max_price: Decimal,
    now: datetime,
    diagnostics_dir: Path,
) -> dict:
    record = _date_record(travel_day)
    record["searched_independently"] = True
    page = context.new_page()
    try:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        if detect_access_block(page):
            raise AccessBlockedError(
                "DB displayed an access restriction on the search page"
            )
        configure_search(
            page,
            travel_day,
            origin=origin,
            destination=destination,
            departure_time="00:00",
        )
        summary = (
            page.locator('[data-test-id="qf-reisende"]')
            .inner_text()
            .replace("\xa0", " ")
        )
        if "Person (15-26 Jahre)" not in summary or "keine Ermäßigung" not in summary:
            raise RuntimeError(f"Passenger configuration was not retained: {summary!r}")
        search = page.get_by_role("button", name="Suchen", exact=True)
        search.wait_for(state="visible", timeout=30000)
        search.click()
        if detect_access_block(page):
            raise AccessBlockedError(
                "DB displayed an access restriction after search submission"
            )
        rows = page.locator("li.verbindung-list__result-item")
        try:
            rows.first.wait_for(state="visible", timeout=45000)
        except PlaywrightTimeoutError:
            body = page.locator("body").inner_text().replace("\xa0", " ").lower()
            if any(
                text in body
                for text in (
                    "keine verbindungen",
                    "keine passenden verbindungen",
                    "keine fahrten",
                )
            ):
                record["status"] = "NOT_AVAILABLE"
                record["errors"].append("DB returned no journeys for this date")
                return record
            raise

        if "kl=2" not in page.url:
            raise RuntimeError(
                f"Second class was not present in the submitted search URL: {page.url}"
            )
        search_url = page.url
        record["search_url"] = search_url
        collected: dict[tuple[str, str, str, tuple[str, ...]], JourneyOffer] = {}
        no_growth_count = 0
        for page_number in range(MAX_PAGES_PER_DAY):
            record["pages_inspected"] = page_number + 1
            page_offers, next_day_seen = _scan_page_for_day(
                page, travel_day, origin, destination, now
            )
            collected.update(page_offers)
            if next_day_seen:
                record["full_day_coverage_verified"] = True
                record["status"] = "COMPLETE"
                break

            later = page.get_by_role(
                "button", name=re.compile("Spätere Verbindungen", re.IGNORECASE)
            )
            if not later.count():
                record["limitations"].append(
                    "The later-connections control disappeared before the next date boundary was observed."
                )
                record["status"] = "PARTIAL"
                break
            if not later.first.is_enabled():
                record["full_day_coverage_verified"] = True
                record["status"] = "COMPLETE"
                break

            before = tuple(rows.all_text_contents())
            later.first.click(timeout=10000)
            deadline = time.monotonic() + PAGE_ADVANCE_TIMEOUT_MS / 1000
            changed = False
            while time.monotonic() < deadline:
                page.wait_for_timeout(250)
                if tuple(rows.all_text_contents()) != before:
                    changed = True
                    break
            if not changed:
                later = page.get_by_role(
                    "button", name=re.compile("Spätere Verbindungen", re.IGNORECASE)
                )
                if later.count() and later.first.is_enabled():
                    no_growth_count += 1
                    if no_growth_count >= 2:
                        record["limitations"].append(
                            "Later-connections pagination stalled twice before the next date boundary."
                        )
                        record["status"] = "PARTIAL"
                        break
                else:
                    record["limitations"].append(
                        "The later-connections control ended without exposing the next date boundary."
                    )
                    record["status"] = "PARTIAL"
                    break
            else:
                no_growth_count = 0
        else:
            record["status"] = "PARTIAL"
            record["limitations"].append(
                f"Pagination reached the safety limit of {MAX_PAGES_PER_DAY} pages."
            )

        offers = list(collected.values())
        for offer in offers:
            offer.url = search_url
        record["journeys"] = [_serialize_offer(offer) for offer in offers]
        record["journeys_inspected"] = len(offers)
        prices = [offer.price for offer in offers if offer.price is not None]
        record["lowest_observed_fare"] = str(min(prices)) if prices else None
        record["qualifying_journeys"] = [
            _serialize_offer(offer)
            for offer in offers
            if is_qualifying_fare(offer.price, offer.fare_confirmed, max_price)
        ]
        unconfirmed_low_fares = [
            offer
            for offer in offers
            if offer.price is not None
            and offer.price < max_price
            and not offer.fare_confirmed
        ]
        if unconfirmed_low_fares:
            record["limitations"].append(
                f"{len(unconfirmed_low_fares)} starting fare(s) below the threshold require checkout confirmation; none were treated as qualifying."
            )
            record["status"] = "PARTIAL"
        if any(offer.price is None for offer in offers):
            record["limitations"].append(
                "At least one ICE-only journey had no parseable displayed fare."
            )
            record["status"] = "PARTIAL"
        if offers:
            record["limitations"].append(
                "Displayed fares are from the result card; checkout was not entered unless a fare is explicitly confirmed."
            )
        return record
    except AccessBlockedError:
        _save_failure_artifacts(page, travel_day, diagnostics_dir)
        raise
    except Exception:
        _save_failure_artifacts(page, travel_day, diagnostics_dir)
        raise
    finally:
        page.close()


def _summary(report: dict, finished_at: datetime | None = None) -> dict:
    dates = report["dates"]
    statuses = ("COMPLETE", "PARTIAL", "FAILED", "NOT_AVAILABLE")
    status_counts = {
        status: sum(item["status"] == status for item in dates) for status in statuses
    }
    qualifying = [offer for item in dates for offer in item["qualifying_journeys"]]
    qualifying_prices = [
        Decimal(offer["price"])
        for offer in qualifying
        if offer.get("price") is not None
    ]
    observed_prices = [
        Decimal(offer["price"])
        for item in dates
        for offer in item["journeys"]
        if offer.get("price") is not None
    ]
    start = datetime.fromisoformat(report["run_start"])
    finish = finished_at or (
        datetime.fromisoformat(report["run_finish"])
        if report.get("run_finish")
        else None
    )
    all_complete = bool(dates) and all(
        item["status"] == "COMPLETE" and item["full_day_coverage_verified"]
        for item in dates
    )
    all_days_covered = bool(dates) and all(
        item["full_day_coverage_verified"] for item in dates
    )
    return {
        "run_status": "COMPLETE" if all_complete else "INCOMPLETE",
        "run_start": report["run_start"],
        "run_finish": finish.isoformat(timespec="seconds") if finish else None,
        "elapsed_seconds": round((finish - start).total_seconds(), 1)
        if finish
        else None,
        "expected_dates": len(dates),
        "completed_dates": status_counts["COMPLETE"],
        "partial_dates": status_counts["PARTIAL"],
        "failed_dates": status_counts["FAILED"],
        "not_available_dates": status_counts["NOT_AVAILABLE"],
        "total_ice_only_journeys_inspected": sum(
            item["journeys_inspected"] for item in dates
        ),
        "cheapest_observed_ice_fare": str(min(observed_prices))
        if observed_prices
        else None,
        "cheapest_qualifying_fare": str(min(qualifying_prices))
        if qualifying_prices
        else None,
        "qualifying_journeys_below_threshold": len(qualifying),
        "full_day_coverage_verified": all_days_covered,
        "threshold_eur_strictly_less_than": report["max_price"],
        "telegram_status": report.get("telegram_status", "NOT_REQUESTED"),
    }


def _markdown_report(report: dict) -> str:
    summary = _summary(report)
    lines = [
        "# Deutsche Bahn ICE Fare Scan",
        "",
        f"- Run status: **{summary['run_status']}**",
        f"- Started (Europe/Berlin): {summary['run_start']}",
        f"- Finished (Europe/Berlin): {summary['run_finish'] or 'in progress'}",
        f"- Expected dates: {summary['expected_dates']}",
        f"- COMPLETE / PARTIAL / FAILED / NOT_AVAILABLE: {summary['completed_dates']} / {summary['partial_dates']} / {summary['failed_dates']} / {summary['not_available_dates']}",
        f"- ICE-only journeys inspected: {summary['total_ice_only_journeys_inspected']}",
        f"- Cheapest observed ICE fare: {summary['cheapest_observed_ice_fare'] or 'none observed'} EUR",
        f"- Cheapest confirmed qualifying fare: {summary['cheapest_qualifying_fare'] or 'none observed'} EUR",
        f"- Confirmed qualifying journeys (< €{report['max_price']}): {summary['qualifying_journeys_below_threshold']}",
        f"- Full-day coverage verified for every date: {summary['full_day_coverage_verified']}",
        f"- Telegram: {summary['telegram_status']}",
        "",
        "## Date-by-date results",
        "",
        "| Date | Status | ICE-only journeys | Lowest displayed fare | Under threshold | Coverage |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for item in report["dates"]:
        fare = (
            f"€{item['lowest_observed_fare']}"
            if item["lowest_observed_fare"] is not None
            else "—"
        )
        coverage = "verified" if item["full_day_coverage_verified"] else "not verified"
        lines.append(
            f"| {item['date']} | {item['status']} | {item['journeys_inspected']} | {fare} | {len(item['qualifying_journeys'])} | {coverage} |"
        )
        for error in item["errors"]:
            lines.append(f"  - Error: {error}")
        for limitation in item["limitations"]:
            lines.append(f"  - Limitation: {limitation}")

    lines.extend(["", "## Confirmed qualifying journeys", ""])
    qualifying = [
        offer for item in report["dates"] for offer in item["qualifying_journeys"]
    ]
    if not qualifying:
        lines.append("No confirmed displayed fares below the threshold were observed.")
        if summary["run_status"] != "COMPLETE":
            lines.append(
                "This is not a no-ticket conclusion: at least one date was not fully checked."
            )
    for offer in qualifying:
        lines.extend(
            [
                f"- **{offer['day']} {offer['departure_time']}–{offer['arrival_time']}**, €{offer['price']}",
                f"  Trains: {', '.join(offer['train_numbers'])}; transfers: {offer['transfers']}; duration: {offer['duration']}",
                f"  Route: {offer['route']}; observed: {offer['observed_at']}",
                f"  Search: {offer['url']}",
            ]
        )
    lines.extend(
        [
            "",
            "Starting fares labeled ‘ab’ are recorded but are not treated as confirmed checkout fares or alertable tickets.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_reports(report: dict, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    report["summary"] = _summary(report)
    stem = f"bahn-scan-{report['run_id']}"
    json_path = report_dir / f"{stem}.json"
    markdown_path = report_dir / f"{stem}.md"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    markdown = _markdown_report(report)
    for path, content in (
        (json_path, payload),
        (markdown_path, markdown),
        (report_dir / "latest.json", payload),
        (report_dir / "latest.md", markdown),
    ):
        path.write_text(content + "\n", encoding="utf-8")
    return json_path, markdown_path


def run_monitor(
    start_day: date | None = None,
    end_day: date = DEFAULT_END_DATE,
    *,
    origin: str = "München Hbf",
    destination: str = "Berlin Hbf",
    max_price: Decimal = Decimal("20.00"),
    report_dir: Path = DEFAULT_REPORT_DIR,
    max_runtime_seconds: int = 52 * 60,
    inter_date_delay: float = INTER_DATE_DELAY_SECONDS,
) -> dict:
    started = berlin_now()
    first_day = max(start_day or started.date(), started.date())
    days = generate_dates(first_day, end_day)
    run_id = started.strftime("%Y%m%dT%H%M%S%z")
    report = {
        "run_id": run_id,
        "timezone": "Europe/Berlin",
        "origin": origin,
        "destination": destination,
        "passengers": 1,
        "age_category": "15-26",
        "discount_card": None,
        "class": 2,
        "departure_start_time": "00:00",
        "max_price": str(max_price),
        "run_start": started.isoformat(timespec="seconds"),
        "run_finish": None,
        "telegram_status": "NOT_REQUESTED",
        "dates": [_date_record(day, reason="Not attempted yet") for day in days],
    }
    _write_reports(report, report_dir)
    deadline = time.monotonic() + max_runtime_seconds
    stop_reason: str | None = None
    browser = None
    context = None
    playwright = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1200},
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        for index, day in enumerate(days):
            if time.monotonic() >= deadline:
                stop_reason = f"Run time budget ({max_runtime_seconds}s) expired before this date."
                report["dates"][index] = _date_record(day, "NOT_AVAILABLE", stop_reason)
                for remaining_index in range(index + 1, len(days)):
                    report["dates"][remaining_index] = _date_record(
                        days[remaining_index],
                        "NOT_AVAILABLE",
                        "Not attempted because the run time budget expired.",
                    )
                break

            result = None
            for attempt in range(1, MAX_ATTEMPTS_PER_DATE + 1):
                try:
                    result = _search_one_date(
                        context,
                        day,
                        origin=origin,
                        destination=destination,
                        max_price=max_price,
                        now=started,
                        diagnostics_dir=report_dir / "diagnostics",
                    )
                    break
                except AccessBlockedError as exc:
                    result = _date_record(day, "FAILED", str(exc))
                    result["searched_independently"] = True
                    stop_reason = f"Stopped after access restriction on {day.isoformat()}; no further requests were made."
                    break
                except (PlaywrightError, RuntimeError, OSError, ValueError) as exc:
                    if attempt == MAX_ATTEMPTS_PER_DATE:
                        result = _date_record(
                            day, "FAILED", f"{type(exc).__name__}: {exc}"
                        )
                        result["searched_independently"] = True
                    else:
                        time.sleep(min(2**attempt, 8))

            report["dates"][index] = result or _date_record(
                day, "FAILED", "Search returned no status."
            )
            report["run_finish"] = berlin_now().isoformat(timespec="seconds")
            _write_reports(report, report_dir)
            item = report["dates"][index]
            print(
                f"DATE {day.isoformat()} {item['status']} journeys={item['journeys_inspected']} "
                f"pages={item['pages_inspected']} coverage={item['full_day_coverage_verified']}",
                flush=True,
            )
            if stop_reason:
                for remaining_index in range(index + 1, len(days)):
                    report["dates"][remaining_index] = _date_record(
                        days[remaining_index], "FAILED", stop_reason
                    )
                break
            if index + 1 < len(days):
                time.sleep(inter_date_delay)
    except Exception as exc:  # noqa: BLE001 - preserve a final report for unexpected run failures
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        for index, day in enumerate(days):
            if report["dates"][index]["status"] == "NOT_AVAILABLE" and report["dates"][
                index
            ]["errors"] == ["Not attempted yet"]:
                report["dates"][index] = _date_record(
                    day, "FAILED", report["fatal_error"]
                )
    finally:
        if context is not None:
            try:
                context.tracing.stop(path=str(report_dir / f"bahn-trace-{run_id}.zip"))
            except (OSError, PlaywrightError) as exc:
                report.setdefault("diagnostic_errors", []).append(
                    f"Trace save failed: {exc}"
                )
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        report["run_finish"] = berlin_now().isoformat(timespec="seconds")
        _write_reports(report, report_dir)
    return report


def send_telegram_notification(
    token: str, chat_id: str, offer: JourneyOffer
) -> requests.Response:
    payload = {
        "chat_id": chat_id,
        "text": offer.as_telegram_message(),
        "disable_web_page_preview": True,
    }
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=20
    )
    response.raise_for_status()
    return response


def send_telegram_test(token: str, chat_id: str) -> requests.Response:
    payload = {
        "chat_id": chat_id,
        "text": "DB fare monitor test: Telegram delivery is configured. No fare alert is being sent.",
        "disable_web_page_preview": True,
    }
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=20
    )
    response.raise_for_status()
    return response


def telegram_notification_if_needed(
    offers: Iterable[JourneyOffer], *, token: str, chat_id: str, max_price: Decimal
) -> int:
    sent = 0
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for offer in offers:
        key = dedupe_key(offer)
        if key in seen or not is_qualifying_fare(
            offer.price, offer.fare_confirmed, max_price
        ):
            continue
        seen.add(key)
        send_telegram_notification(token, chat_id, offer)
        sent += 1
    return sent


def _offers_from_report(report: dict) -> list[JourneyOffer]:
    offers = []
    for item in report["dates"]:
        for raw in item["qualifying_journeys"]:
            offer = dict(raw)
            offer["price"] = Decimal(offer["price"])
            offers.append(JourneyOffer(**offer))
    return offers


def _send_telegram_summary(report: dict, token: str, chat_id: str) -> None:
    summary = _summary(report)
    incomplete = [
        item["date"] for item in report["dates"] if item["status"] != "COMPLETE"
    ]
    incomplete_text = ", ".join(incomplete) if incomplete else "none"
    cheapest = summary["cheapest_observed_ice_fare"]
    fare_text = f"€{cheapest}" if cheapest else "none observed"
    conclusion = (
        "No conclusion about unchecked/partial dates."
        if incomplete
        else "Every expected date has verified full-day coverage."
    )
    message = (
        f"DB hourly scan {summary['run_status']}\n"
        f"Dates complete: {summary['completed_dates']}/{summary['expected_dates']}\n"
        f"Partial/failed/unavailable dates: {incomplete_text}\n"
        f"Cheapest observed ICE fare: {fare_text}\n"
        f"Confirmed qualifying journeys below €20: {summary['qualifying_journeys_below_threshold']}\n"
        f"{conclusion}"
    )
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()


@contextmanager
def run_lock():
    lock_path = Path(os.getenv("DB_MONITOR_LOCK", "/tmp/db-monitor.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another monitor run holds {lock_path}") from exc
        yield


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hourly DB ICE fare monitor")
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        help="Optional start date; past dates are always excluded",
    )
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END_DATE)
    parser.add_argument("--origin", default="München Hbf")
    parser.add_argument("--destination", default="Berlin Hbf")
    parser.add_argument("--max-price", type=Decimal, default=Decimal("20.00"))
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--max-runtime-minutes", type=int, default=52)
    parser.add_argument(
        "--telegram-test",
        action="store_true",
        help="Send a safe Telegram test; suppress fare alerts",
    )
    parser.add_argument(
        "--send-telegram-alerts",
        action="store_true",
        help="Send confirmed fare alerts and an hourly summary",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_cli_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if args.telegram_test:
        if not token or not chat_id:
            print(
                "Telegram test requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
                file=sys.stderr,
            )
            return 2
        try:
            response = send_telegram_test(token, chat_id)
            print(f"TELEGRAM_TEST_SENT http_status={response.status_code}")
        except requests.RequestException as exc:
            print(f"TELEGRAM_TEST_FAILED {exc}", file=sys.stderr)
            return 2
        return 0

    try:
        with run_lock():
            report = run_monitor(
                args.start,
                args.end,
                origin=args.origin,
                destination=args.destination,
                max_price=args.max_price,
                report_dir=args.report_dir,
                max_runtime_seconds=max(60, args.max_runtime_minutes * 60),
            )
    except RuntimeError as exc:
        print(f"MONITOR LOCKED: {exc}", file=sys.stderr)
        return 3

    if args.send_telegram_alerts:
        if not token or not chat_id:
            report["telegram_status"] = "FAILED: Telegram secrets are missing"
        else:
            try:
                sent = telegram_notification_if_needed(
                    _offers_from_report(report),
                    token=token,
                    chat_id=chat_id,
                    max_price=args.max_price,
                )
                _send_telegram_summary(report, token, chat_id)
                report["telegram_status"] = (
                    f"SENT: {sent} confirmed fare alert(s) and summary"
                )
            except requests.RequestException as exc:
                report["telegram_status"] = f"FAILED: {exc}"
    else:
        report["telegram_status"] = "DISABLED"

    _write_reports(report, args.report_dir)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    telegram_failed = args.send_telegram_alerts and not report[
        "telegram_status"
    ].startswith("SENT:")
    return (
        0
        if report["summary"]["run_status"] == "COMPLETE"
        and not report.get("fatal_error")
        and not telegram_failed
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())

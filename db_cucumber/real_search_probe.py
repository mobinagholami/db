from __future__ import annotations

import os
import re
import time
from datetime import date

from playwright.sync_api import sync_playwright

BASE_URL = "https://www.bahn.de/"


def log(msg: str):
    print(msg, flush=True)


def wait_for_page_stable(page, seconds: float = 2.0):
    time.sleep(seconds)


def select_station(page, input_name: str, station: str, label: str):
    locator = page.locator(f"input[name='{input_name}']")
    log(f"{label}: waiting for input ...")
    locator.wait_for(state="visible", timeout=30000)
    locator.fill(station)
    wait_for_page_stable(page, 2)

    option_locator = page.locator('[role="option"]').filter(has_text=station).first
    log(f"{label}: waiting for station suggestion '{station}' ...")
    option_locator.wait_for(state="visible", timeout=20000)
    log(f"{label}: suggestion visible -> {option_locator.inner_text()}")
    option_locator.click()
    wait_for_page_stable(page, 2)

    value = locator.input_value()
    log(f"{label}: selected value = {value!r}")
    assert value == station or station in value, (
        f"Station not selected: {label} -> {value!r}"
    )


def choose_passenger_age_25(page):
    log("Passengers: opening traveler dialog")
    passenger_button = page.locator('[data-test-id="qf-reisende"]')
    passenger_button.wait_for(state="visible", timeout=30000)
    passenger_button.click()
    dialog = page.locator("dialog[open]")
    dialog.wait_for(state="visible", timeout=10000)
    age_control = dialog.locator('button[role="combobox"]').filter(
        has_text="Person (27-64 Jahre)"
    )
    age_control.wait_for(state="visible", timeout=10000)
    log("Passengers: opening the live age combobox")
    age_control.click()
    age_option = page.get_by_role("option", name="Person (15-26 Jahre)", exact=True)
    age_option.wait_for(state="visible", timeout=10000)
    log("Passengers: selecting Person (15-26 Jahre)")
    age_option.click()

    no_discount = dialog.locator('button[role="combobox"]').filter(
        has_text="keine Ermäßigung"
    )
    no_discount.wait_for(state="visible", timeout=10000)
    dialog_summary = dialog.inner_text().replace("\xa0", " ")
    if (
        "Person (15-26 Jahre)" not in dialog_summary
        or "keine Ermäßigung" not in dialog_summary
    ):
        raise RuntimeError(
            f"Passenger dialog did not match the requested values: {dialog_summary!r}"
        )

    log("Passengers: accepting traveler settings")
    dialog.locator('[data-test-id="quick-finder-save-button"]').click()
    wait_for_page_stable(page, 0.5)
    summary = passenger_button.inner_text().replace("\xa0", " ")
    if "Person (15-26 Jahre)" not in summary or "keine Ermäßigung" not in summary:
        raise RuntimeError(
            f"Committed passenger settings did not match the requested values: {summary!r}"
        )


def choose_date(page, target_date: date, departure_time: str = "00:00"):
    log(f"Date: opening outbound journey popup for {target_date.isoformat()}")
    trigger = page.get_by_text("Hinfahrt ändern", exact=True)
    trigger.wait_for(state="visible", timeout=30000)
    trigger.click()

    dialog = page.locator("dialog[open]").last
    dialog.wait_for(state="visible", timeout=10000)
    day = dialog.get_by_role("spinbutton", name="Tag")
    month = dialog.get_by_role("spinbutton", name="Monat")
    year = dialog.get_by_role("spinbutton", name="Jahr")
    hours = dialog.get_by_role("spinbutton", name="Stunden")
    minutes = dialog.get_by_role("spinbutton", name="Minuten")
    day.wait_for(state="visible", timeout=10000)
    year.fill(str(target_date.year))
    month.fill(f"{target_date.month:02d}")
    day.fill(f"{target_date.day:02d}")
    requested_hour, requested_minute = departure_time.split(":", maxsplit=1)
    hours.fill(requested_hour)
    minutes.fill(requested_minute)
    minutes.press("Tab")

    announcement = dialog.locator(".test-aria-date-label")
    expected_date = target_date.strftime("%d.%m.%Y")
    announcement.wait_for(state="visible", timeout=10000)
    selected_date = announcement.inner_text()
    if expected_date not in selected_date:
        raise RuntimeError(
            f"Date picker selected {selected_date!r}, expected {expected_date}"
        )
    if (
        hours.input_value() != requested_hour
        or minutes.input_value() != requested_minute
    ):
        raise RuntimeError(
            f"Date picker time was {hours.input_value()}:{minutes.input_value()}, "
            f"expected {departure_time}"
        )
    log(f"Date: selected {selected_date}")
    log("Date: accepting the selected departure date")
    dialog.get_by_role("button", name="Übernehmen", exact=True).click()
    wait_for_page_stable(page, 0.5)


def configure_search(
    page,
    target_date: date,
    *,
    origin: str = "München Hbf",
    destination: str = "Berlin Hbf",
    departure_time: str = "00:00",
):
    select_station(page, "quickFinderBasic-von", origin, "Origin")
    select_station(page, "quickFinderBasic-nach", destination, "Destination")
    choose_passenger_age_25(page)
    choose_date(page, target_date, departure_time)


def click_search(page):
    log("Search: clicking search button")
    button = page.get_by_role("button", name="Suchen", exact=True)
    button.wait_for(state="visible", timeout=30000)
    button.click()
    wait_for_page_stable(page, 6)


def inspect_after_search(page):
    body_text = page.locator("body").inner_text()
    lower = body_text.lower()
    if (
        "automated bot" in lower
        or "access-error" in lower
        or "errorcode=751" in lower
        or "fehler-code: 751" in lower
    ):
        raise RuntimeError(
            "Live site displayed an access restriction after submitting the search"
        )

    normalized_body = body_text.replace("\xa0", " ")
    train_services = list(dict.fromkeys(re.findall(r"\bICE\s+\d+\b", normalized_body)))
    required_result_text = [
        "München Hbf",
        "Berlin Hbf",
        "Person (15-26 Jahre)",
        "19. Nov. 2026",
    ]
    missing_result_text = [
        text for text in required_result_text if text not in normalized_body
    ]
    if (
        "/buchung/fahrplan/suche" not in page.url
        or not train_services
        or missing_result_text
    ):
        raise RuntimeError(
            "Search did not reach verified journey results: "
            f"url={page.url!r}, train_services={train_services!r}, "
            f"missing={missing_result_text!r}"
        )
    cards = page.locator("li.verbindung-list__result-item")
    log(
        f"Search results: {len(train_services)} ICE service entries across {cards.count()} displayed journeys"
    )
    log(f"First journey: {cards.first.inner_text().splitlines()[0:5]}")


def main():
    with sync_playwright() as p:
        headless = os.getenv("HEADLESS", "1").lower() not in {"0", "false", "no"}
        log(
            f"Starting standalone {'headless' if headless else 'visible'}-browser Bahn probe"
        )
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(
            viewport={"width": 1600, "height": 1200},
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            configure_search(page, date(2026, 11, 19))

            click_search(page)
            inspect_after_search(page)
        finally:
            browser.close()


if __name__ == "__main__":
    main()

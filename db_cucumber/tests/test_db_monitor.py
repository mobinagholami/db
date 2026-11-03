import json
from datetime import date
from decimal import Decimal

from db_monitor import (
    _date_record,
    _write_reports,
    generate_dates,
    is_ice_only_journey,
    is_qualifying_fare,
    parse_displayed_price,
    parse_price,
)


def test_parse_price_in_german_format():
    assert parse_price("19,99 €") == Decimal("19.99")
    assert parse_price("14,90 €") == Decimal("14.90")
    assert parse_price("9,99 €") == Decimal("9.99")


def test_parse_price_parses_20_euro_exactly():
    assert parse_price("20,00 €") == Decimal("20.00")


def test_result_card_parser_uses_displayed_new_price_and_marks_starting_fare():
    price, is_from = parse_displayed_price(
        "Alter Preis 90,99 € durchgestrichen, neuer Preis ab 19,99 €"
    )
    assert price == Decimal("19.99")
    assert is_from is True


def test_strict_threshold_requires_confirmed_fare():
    assert is_qualifying_fare(Decimal("19.99"), True) is True
    assert is_qualifying_fare(Decimal("20.00"), True) is False
    assert is_qualifying_fare(Decimal("19.99"), False) is False
    assert is_qualifying_fare(None, False) is False


def test_date_generation_includes_each_day_and_end_date():
    assert generate_dates(date(2026, 11, 3), date(2026, 11, 6)) == [
        date(2026, 11, 3),
        date(2026, 11, 4),
        date(2026, 11, 5),
        date(2026, 11, 6),
    ]
    assert generate_dates(date(2027, 1, 1), date(2026, 12, 31)) == []


def test_reports_include_each_date_and_keep_partial_runs_incomplete(tmp_path):
    day = _date_record(
        date(2026, 11, 3), "PARTIAL", "Starting fare needs checkout confirmation"
    )
    day["searched_independently"] = True
    day["full_day_coverage_verified"] = True
    day["journeys_inspected"] = 14
    report = {
        "run_id": "test-run",
        "run_start": "2026-11-03T10:00:00+01:00",
        "run_finish": "2026-11-03T10:01:00+01:00",
        "max_price": "20.00",
        "telegram_status": "DISABLED",
        "dates": [day],
    }

    json_path, markdown_path = _write_reports(report, tmp_path)

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert saved["summary"]["run_status"] == "INCOMPLETE"
    assert saved["summary"]["partial_dates"] == 1
    assert saved["dates"][0]["date"] == "2026-11-03"
    assert "PARTIAL" in markdown
    assert "checkout confirmation" in markdown


def test_ice_only_journey_accepts_direct_ice_and_ice_transfers():
    assert is_ice_only_journey("ICE 101 09:00 München Hbf 11:00 Berlin Hbf") is True
    assert is_ice_only_journey("ICE 1 München Hbf → ICE 5 Berlin Hbf") is True


def test_ice_only_journey_rejects_regional_connections():
    assert (
        is_ice_only_journey("ICE 1 München Hbf → RE 456 Nürnberg → ICE 10 Berlin Hbf")
        is False
    )
    assert is_ice_only_journey("ICE 1 München Hbf → RB 12 → Berlin Hbf") is False
    assert is_ice_only_journey("ICE 1 München Hbf → IC 2 Berlin Hbf") is False
    assert is_ice_only_journey("FLIXTRAIN 123 München Hbf → Berlin Hbf") is False

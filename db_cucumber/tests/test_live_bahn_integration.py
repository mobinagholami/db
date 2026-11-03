from datetime import date

import pytest

from db_monitor import run_monitor


@pytest.mark.live
def test_live_bahn_search_covers_one_date_and_returns_ice_rows(tmp_path):
    report = run_monitor(
        date(2026, 11, 19),
        date(2026, 11, 19),
        origin="München Hbf",
        destination="Berlin Hbf",
        report_dir=tmp_path,
        max_runtime_seconds=180,
        inter_date_delay=0,
    )

    result = report["dates"][0]
    assert result["searched_independently"] is True
    assert result["status"] in {"COMPLETE", "PARTIAL"}
    assert result["full_day_coverage_verified"] is True
    assert result["journeys_inspected"] > 0
    assert all(
        journey["train_numbers"]
        and all(number.startswith("ICE ") for number in journey["train_numbers"])
        for journey in result["journeys"]
    )
    if result["status"] == "PARTIAL":
        assert any(
            "checkout confirmation" in limitation
            for limitation in result["limitations"]
        )

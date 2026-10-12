"""The reports page figures agree with the ledger."""

import pytest
from fastapi.testclient import TestClient
from test_reports_support import seed_reports

OCTOBER = {"period": "custom", "start": "2026-10-01", "end": "2026-10-31"}


def _october(client: TestClient, path: str = "/reports") -> str:
    return client.get(path, params=OCTOBER).text


def _summary_card(page: str, label: str) -> str:
    start = page.index(f'<p class="text-sm text-secondary">{label}</p>')
    return page[start : page.index("</div>", start)]


@pytest.fixture
def seeded_client(signed_in: TestClient) -> TestClient:
    seed_reports(signed_in.app)
    return signed_in


def test_the_figures_match_the_ledger(seeded_client: TestClient) -> None:
    page = _october(seeded_client)

    assert "$3,000.00" in _summary_card(page, "Income")
    assert "$215.01" in _summary_card(page, "Expenses")  # the 100.01 joint bill counts at half
    assert "$2,784.99" in _summary_card(page, "Net")
    assert "92.8%" in _summary_card(page, "Savings rate")

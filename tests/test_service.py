"""Tests de la lógica de negocio con un repositorio en memoria."""

import threading
import time
from datetime import date

import pytest
from helpers import make_row

from gastos_mcp.models import ReceiptItem, SheetRow
from gastos_mcp.repository import InMemoryRepository
from gastos_mcp.service import DuplicateReceiptError, GastosService, normalize

MILK = ReceiptItem(item_name="SENSE LACTOSA SEMIDE", category="Leche", unit_price=0.94)


def service_with(*rows: SheetRow) -> tuple[GastosService, InMemoryRepository]:
    repository = InMemoryRepository(rows)
    return GastosService(repository), repository


def test_normalize_removes_accents_case_and_extra_spaces() -> None:
    assert normalize("  bonÀrea   S.A. ") == "bonarea s.a."


# --- check_receipt ---------------------------------------------------------------


def test_check_receipt_not_found() -> None:
    service, _ = service_with(make_row(invoice_number="A-1"))
    assert service.check_receipt("B-9", "Lidl").exists is False


def test_check_receipt_returns_date_and_item_count() -> None:
    service, _ = service_with(
        make_row(invoice_number="A-1", purchase_date=date(2026, 10, 5)),
        make_row(invoice_number="A-1", purchase_date=date(2026, 10, 5), item_name="HUEVOS"),
    )
    result = service.check_receipt("A-1", "Lidl")
    assert result.exists is True
    assert [(m.purchase_date, m.item_count, m.first_row) for m in result.matches] == [(date(2026, 10, 5), 2, 2)]


def test_check_receipt_ignores_case_and_leading_zeros() -> None:
    # Un nº guardado como número perdió sus ceros: "000123" debe reconocerse igualmente.
    service, _ = service_with(make_row(invoice_number="123"))
    assert service.check_receipt("000123", "lidl").exists is True


def test_check_receipt_accepts_shortened_merchant_name() -> None:
    service, _ = service_with(make_row(merchant="Lidl"))
    assert service.check_receipt("A-1", "LIDL SUPERMERCADOS S.A.U.").exists is True


def test_check_receipt_other_merchant_is_not_a_match() -> None:
    service, _ = service_with(make_row(merchant="Lidl"))
    assert service.check_receipt("A-1", "bonÀrea").exists is False


def test_check_receipt_same_invoice_on_different_dates_is_listed_separately() -> None:
    service, _ = service_with(
        make_row(purchase_date=date(2026, 9, 1)),
        make_row(purchase_date=date(2026, 10, 1)),
    )
    dates = [m.purchase_date for m in service.check_receipt("A-1", "Lidl").matches]
    assert dates == [date(2026, 10, 1), date(2026, 9, 1)]


def test_check_receipt_ignores_old_rows_without_invoice_number() -> None:
    service, _ = service_with(make_row(invoice_number=""))
    assert service.check_receipt("0", "Lidl").exists is False


# --- append_receipt --------------------------------------------------------------


def test_append_writes_one_row_per_item_and_leaves_f_and_g_empty() -> None:
    service, repository = service_with(make_row(invoice_number="OLD"))
    result = service.append_receipt("A-1", date(2026, 10, 14), "Lidl", [MILK, MILK])

    assert result.rows_written == 2
    written = repository.read_rows()[1:]
    assert [
        (r.invoice_number, r.purchase_date, r.merchant, r.item_name, r.category, r.unit_price) for r in written
    ] == [("A-1", date(2026, 10, 14), "Lidl", "SENSE LACTOSA SEMIDE", "Leche", 0.94)] * 2
    assert all(r.pack is None and r.description == "" for r in written)
    assert [r.row_number for r in written] == [3, 4]


def test_append_rejects_duplicate_and_writes_nothing() -> None:
    service, repository = service_with(make_row(invoice_number="A-1", purchase_date=date(2026, 10, 5)))
    with pytest.raises(DuplicateReceiptError, match="05/10/2026"):
        service.append_receipt("A-1", date(2026, 10, 5), "Lidl", [MILK])
    assert len(repository.read_rows()) == 1


def test_append_rejects_same_invoice_even_with_different_date() -> None:
    # D9: la clave no incluye la fecha (una fecha mal leída no debe colar un duplicado).
    service, _ = service_with(make_row(invoice_number="A-1", purchase_date=date(2026, 10, 5)))
    with pytest.raises(DuplicateReceiptError):
        service.append_receipt("A-1", date(2026, 10, 6), "Lidl", [MILK])


def test_append_allows_existing_invoice_when_explicitly_confirmed() -> None:
    service, repository = service_with(make_row(invoice_number="A-1"))
    service.append_receipt("A-1", date(2026, 10, 6), "Lidl", [MILK], allow_existing_invoice=True)
    assert len(repository.read_rows()) == 2


def test_append_same_invoice_other_merchant_is_allowed() -> None:
    service, repository = service_with(make_row(invoice_number="A-1", merchant="Lidl"))
    service.append_receipt("A-1", date(2026, 10, 6), "bonÀrea", [MILK])
    assert len(repository.read_rows()) == 2


def test_append_reuses_existing_category_spelling() -> None:
    service, repository = service_with(make_row(category="Leche"), make_row(category="Leche"))
    result = service.append_receipt(
        "B-1", date(2026, 10, 6), "Lidl", [ReceiptItem(item_name="LLET", category="leche", unit_price=1.0)]
    )
    assert result.new_categories == []
    assert repository.read_rows()[-1].category == "Leche"


def test_append_reports_each_new_category_once() -> None:
    service, _ = service_with(make_row(category="Leche"))
    items = [
        ReceiptItem(item_name="A", category="Pescado", unit_price=1.0),
        ReceiptItem(item_name="B", category="pescado", unit_price=2.0),
        ReceiptItem(item_name="C", category="Leche", unit_price=3.0),
    ]
    result = service.append_receipt("B-1", date(2026, 10, 6), "Lidl", items)
    assert result.new_categories == ["Pescado"]
    assert [row.category for row in result.rows] == ["Pescado", "Pescado", "Leche"]


class SlowRepository(InMemoryRepository):
    """Simula la latencia de la API entre la lectura y la escritura."""

    def read_rows(self) -> list[SheetRow]:
        rows = super().read_rows()
        time.sleep(0.05)
        return rows


def test_concurrent_appends_of_the_same_receipt_write_it_once() -> None:
    repository = SlowRepository()
    service = GastosService(repository)
    outcomes: list[str] = []

    def worker() -> None:
        try:
            service.append_receipt("A-1", date(2026, 10, 6), "Lidl", [MILK])
            outcomes.append("written")
        except DuplicateReceiptError:
            outcomes.append("duplicate")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["duplicate"] * 3 + ["written"]
    assert len(repository.read_rows()) == 1


# --- search_prices ---------------------------------------------------------------


@pytest.fixture
def butter_service() -> GastosService:
    service, _ = service_with(
        make_row(
            item_name="MANTEGA A",
            category="Mantequilla",
            merchant="bonÀrea",
            purchase_date=date(2026, 9, 1),
            unit_price=2.40,
        ),
        make_row(
            item_name="MANTEGA B",
            category="Mantequilla",
            merchant="bonÀrea",
            purchase_date=date(2026, 10, 7),
            unit_price=2.45,
        ),
        make_row(
            item_name="MANTEGA C",
            category="Mantequilla",
            merchant="Lidl",
            purchase_date=date(2026, 10, 12),
            unit_price=2.19,
        ),
        make_row(
            item_name="LLET", category="Leche", merchant="Lidl", purchase_date=date(2026, 10, 12), unit_price=0.94
        ),
    )
    return service


def test_search_by_category_and_merchant_returns_most_recent_first(butter_service: GastosService) -> None:
    result = butter_service.search_prices(category="mantequilla", merchant="bonÀrea")
    assert [row.item_name for row in result.rows] == ["MANTEGA B", "MANTEGA A"]


def test_search_merchant_ignores_accents_and_case(butter_service: GastosService) -> None:
    assert butter_service.search_prices(category="Mantequilla", merchant="BONAREA").total_matches == 2


def test_search_query_needs_all_words_across_name_category_and_description() -> None:
    service, _ = service_with(
        make_row(
            item_name="SENSE LACTOSA SEMIDE", category="Leche", description="Leche semidescremada sin lactosa - 1 L"
        ),
        make_row(item_name="LLET ENTERA", category="Leche", description="Leche entera - 1 L"),
    )
    assert service.search_prices(query="lactosa leche").total_matches == 1
    assert service.search_prices(query="lactosa pescado").total_matches == 0


def test_search_reports_total_matches_beyond_the_limit(butter_service: GastosService) -> None:
    result = butter_service.search_prices(category="Mantequilla", limit=1)
    assert (result.total_matches, len(result.rows), result.rows[0].item_name) == (3, 1, "MANTEGA C")


def test_search_requires_query_or_category(butter_service: GastosService) -> None:
    with pytest.raises(ValueError, match="query"):
        butter_service.search_prices(merchant="Lidl")


def test_search_puts_rows_without_a_date_last() -> None:
    service, _ = service_with(
        make_row(item_name="SIN FECHA", purchase_date=None),
        make_row(item_name="CON FECHA", purchase_date=date(2020, 1, 1)),
    )
    assert [row.item_name for row in service.search_prices(category="Leche").rows] == ["CON FECHA", "SIN FECHA"]


# --- search_prices con latest_per_merchant (chat 3) -------------------------------


def test_latest_per_merchant_returns_the_most_recent_purchase_of_each_merchant() -> None:
    # Tres compras recientes en Lidl no deben tapar la de bonÀrea, aunque limit sea pequeño.
    service, _ = service_with(
        make_row(
            item_name="MANTEGA BON",
            merchant="bonÀrea",
            category="Mantequilla",
            purchase_date=date(2026, 9, 1),
            unit_price=2.40,
        ),
        make_row(
            item_name="MANTEGA L1",
            merchant="Lidl",
            category="Mantequilla",
            purchase_date=date(2026, 10, 1),
            unit_price=2.10,
        ),
        make_row(
            item_name="MANTEGA L2",
            merchant="Lidl",
            category="Mantequilla",
            purchase_date=date(2026, 10, 8),
            unit_price=2.15,
        ),
        make_row(
            item_name="MANTEGA L3",
            merchant="Lidl",
            category="Mantequilla",
            purchase_date=date(2026, 10, 12),
            unit_price=2.19,
        ),
    )
    result = service.search_prices(category="Mantequilla", limit=2, latest_per_merchant=True)
    assert [(row.merchant, row.item_name) for row in result.rows] == [
        ("Lidl", "MANTEGA L3"),
        ("bonÀrea", "MANTEGA BON"),
    ]
    assert result.total_matches == 2


def test_latest_per_merchant_groups_spellings_but_not_different_stores() -> None:
    service, _ = service_with(
        make_row(item_name="OLD", merchant="bonArea", purchase_date=date(2026, 9, 1)),
        make_row(item_name="NEW", merchant="BONÀREA", purchase_date=date(2026, 10, 1)),
        make_row(item_name="EXPRESS", merchant="Lidl Express", purchase_date=date(2026, 9, 5)),
        make_row(item_name="LIDL", merchant="Lidl", purchase_date=date(2026, 9, 4)),
    )
    result = service.search_prices(category="Leche", latest_per_merchant=True)
    assert [row.item_name for row in result.rows] == ["NEW", "EXPRESS", "LIDL"]


def test_latest_per_merchant_is_off_by_default(butter_service: GastosService) -> None:
    assert butter_service.search_prices(category="Mantequilla").total_matches == 3


# --- list_categories -------------------------------------------------------------


def test_list_categories_merges_spellings_and_counts_usage() -> None:
    service, _ = service_with(
        make_row(category="Leche"),
        make_row(category="Leche"),
        make_row(category="leche"),
        make_row(category="Pescado"),
        make_row(category="Cafè"),
        make_row(category="Cafe"),
        make_row(category=""),
    )
    assert [(c.name, c.count) for c in service.list_categories()] == [("Cafè", 2), ("Leche", 3), ("Pescado", 1)]

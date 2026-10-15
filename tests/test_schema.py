"""Tests del esquema de columnas y de las constantes derivadas.

Los valores esperados son literales a propósito: son el contrato con la hoja real. Si
importaran las constantes del código, un error en ellas haría pasar el test igualmente.
"""

from datetime import date

import pytest
from helpers import make_row

from gastos_mcp.models import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT
from gastos_mcp.repository import (
    A1_RANGE,
    COLUMN_COUNT,
    Column,
    InMemoryRepository,
    parse_sheet_values,
    row_to_cells,
)
from gastos_mcp.service import GastosService


def test_columns_are_the_eight_sheet_columns_in_order() -> None:
    assert [column.name for column in Column] == [
        "INVOICE_NUMBER", "PURCHASE_DATE", "MERCHANT", "ITEM_NAME", "CATEGORY", "PACK", "DESCRIPTION", "UNIT_PRICE",
    ]  # fmt: skip
    assert [column.letter for column in Column] == list("ABCDEFGH")


def test_column_values_are_contiguous_from_zero() -> None:
    # Sin huecos ni repetidos: son índices directos sobre la lista de celdas.
    assert [column.value for column in Column] == list(range(8))


def test_derived_constants_match_the_real_sheet_layout() -> None:
    assert COLUMN_COUNT == 8
    assert A1_RANGE == "'Gastos - Supermercado'!A:H"


def test_in_memory_repository_reports_ranges_like_the_sheet() -> None:
    repository = InMemoryRepository()
    assert repository.append_rows([make_row(), make_row()]) == "'Gastos - Supermercado'!A2:H3"
    assert repository.append_rows([make_row()]) == "'Gastos - Supermercado'!A4:H4"


def test_row_to_cells_places_each_value_in_its_column() -> None:
    cells = row_to_cells(make_row(invoice_number="X", purchase_date=date(1899, 12, 31), unit_price=2.5))
    assert len(cells) == COLUMN_COUNT
    assert cells[Column.INVOICE_NUMBER] == "X"
    assert cells[Column.PURCHASE_DATE] == 1
    assert cells[Column.UNIT_PRICE] == 2.5
    assert cells[Column.PACK] == "" and cells[Column.DESCRIPTION] == ""


def test_parse_and_write_use_the_same_column_positions() -> None:
    # Lo que se escribe, leído de vuelta con el parser, devuelve los mismos valores.
    original = make_row(invoice_number="000123", purchase_date=date(2026, 10, 14), unit_price=0.94)
    header = ["No. de factura"] + [""] * 7
    parsed = parse_sheet_values([header, row_to_cells(original)])[0]
    assert (parsed.invoice_number, parsed.purchase_date, parsed.merchant, parsed.item_name) == (
        "000123", date(2026, 10, 14), "Lidl", "LLET",
    )  # fmt: skip
    assert (parsed.category, parsed.unit_price) == ("Leche", 0.94)


def test_default_search_limit_is_ten_and_within_the_maximum() -> None:
    assert DEFAULT_SEARCH_LIMIT == 10
    assert DEFAULT_SEARCH_LIMIT <= MAX_SEARCH_LIMIT


@pytest.mark.parametrize("extra", [0, 3])
def test_search_without_limit_returns_the_default_number_of_rows(extra: int) -> None:
    rows = [make_row(item_name=f"ITEM {number}", invoice_number=f"A-{number}") for number in range(10 + extra)]
    service = GastosService(InMemoryRepository(rows))
    result = service.search_prices(category="Leche")
    assert (len(result.rows), result.total_matches) == (10, 10 + extra)

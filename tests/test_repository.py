"""Tests de la conversión hoja <-> modelo y de las peticiones a la API (cliente de Google simulado)."""

from datetime import date

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from helpers import FakeSheetsService, make_row

from gastos_mcp.repository import (
    GoogleSheetsRepository,
    SheetLayoutError,
    SheetsError,
    date_to_serial,
    parse_sheet_values,
    row_to_cells,
    serial_to_date,
)

HEADER = ["No. de factura", "Fecha", "Comercio", "Nombre ítem comercio", "Clasificación ítem", "Pack", "Descripción ítem", "Precio unit.", "Precio total"]


def test_date_serial_conversion_matches_the_value_verified_in_the_real_sheet() -> None:
    # D17: el 14/10/2026 se guardó en la hoja real como la serie 46309.
    assert date_to_serial(date(2026, 10, 14)) == 46309
    assert serial_to_date(46309) == date(2026, 10, 14)


# --- lectura ----------------------------------------------------------------------


def test_parse_reads_typed_cells() -> None:
    rows = parse_sheet_values(
        [HEADER, ["L-1001", 46300, "Lidl", "SENSE LACTOSA SEMIDE", "Leche", 1, "Leche sin lactosa - 1 L", 0.94, 1.88]]
    )
    assert len(rows) == 1
    row = rows[0]
    assert (row.row_number, row.invoice_number, row.purchase_date) == (2, "L-1001", serial_to_date(46300))
    assert (row.merchant, row.item_name, row.category, row.pack, row.unit_price) == (
        "Lidl", "SENSE LACTOSA SEMIDE", "Leche", 1.0, 0.94,
    )
    assert row.total_price == 1.88


def test_parse_pads_short_rows_and_accepts_old_rows_without_invoice() -> None:
    rows = parse_sheet_values([HEADER, ["", 46300, "bonArea", "FARINA", "", "", "", 0.71], ["", 46301, "Lidl"]])
    assert [r.invoice_number for r in rows] == ["", ""]
    assert rows[1].unit_price is None and rows[1].item_name == ""


def test_parse_skips_empty_rows_but_keeps_row_numbers() -> None:
    rows = parse_sheet_values([HEADER, ["A", 46300, "Lidl", "X", "Leche", "", "", 1], [], ["", "", ""], ["B", 46300, "Lidl", "Y", "Leche", "", "", 2]])
    assert [(r.invoice_number, r.row_number) for r in rows] == [("A", 2), ("B", 5)]


def test_parse_invoice_stored_as_number_has_no_decimal_suffix() -> None:
    assert parse_sheet_values([HEADER, [123.0, 46300, "Lidl", "X", "", "", "", 1]])[0].invoice_number == "123"


def test_parse_tolerates_text_dates_and_prices() -> None:
    # D19 pendiente: si alguna fila tiene la fecha o el precio como texto, no debe romper la lectura.
    rows = parse_sheet_values(
        [HEADER, ["A", "05/10/2026", "Lidl", "X", "", "", "", "7,39 €"], ["B", "ayer", "Lidl", "Y", "", "", "", "gratis"]]
    )
    assert (rows[0].purchase_date, rows[0].unit_price) == (date(2026, 10, 5), 7.39)
    assert (rows[1].purchase_date, rows[1].unit_price) == (None, None)


def test_parse_old_rows_without_total_leave_it_empty() -> None:
    # Sheets omite las celdas vacías finales: una fila de 8 (o de 3) celdas se lee sin error.
    rows = parse_sheet_values([HEADER, ["A", 46300, "Lidl", "X", "Leche", "", "", 1], ["B", 46300, "Lidl"]])
    assert [r.total_price for r in rows] == [None, None]


def test_parse_total_tolerates_text_like_the_unit_price() -> None:
    rows = parse_sheet_values(
        [HEADER, ["A", 46300, "Lidl", "X", "", "", "", 1, "7,39 €"], ["B", 46300, "Lidl", "Y", "", "", "", 1, "gratis"]]
    )
    assert [r.total_price for r in rows] == [7.39, None]


@pytest.mark.parametrize("values", [[], [["Fecha", "Comercio"]], [["", "x"]]])
def test_parse_rejects_a_sheet_without_the_current_layout(values: list[list[object]]) -> None:
    with pytest.raises(SheetLayoutError, match="No. de factura"):
        parse_sheet_values(values)


# --- escritura --------------------------------------------------------------------


def test_row_to_cells_uses_native_types_and_leaves_f_and_g_empty() -> None:
    cells = row_to_cells(
        make_row(invoice_number="000123", purchase_date=date(2026, 10, 14), item_name="=1+1", unit_price=0.94, total_price=1.88)
    )
    assert cells == ["000123", 46309, "Lidl", "=1+1", "Leche", "", "", 0.94, 1.88]
    assert type(cells[0]) is str and type(cells[1]) is int and type(cells[7]) is float and type(cells[8]) is float


def test_row_to_cells_requires_date_and_both_prices() -> None:
    for missing in ("purchase_date", "unit_price", "total_price"):
        with pytest.raises(ValueError):
            row_to_cells(make_row(**{missing: None}))


# --- peticiones a la API ----------------------------------------------------------


def test_read_rows_requests_the_tab_range_with_raw_values() -> None:
    fake = FakeSheetsService(values=[HEADER])
    GoogleSheetsRepository("SHEET_ID", service=fake).read_rows()
    assert fake.calls == [
        {
            "method": "get",
            "spreadsheetId": "SHEET_ID",
            "range": "'Gastos - Supermercado'!A:I",
            "valueRenderOption": "UNFORMATTED_VALUE",
            "dateTimeRenderOption": "SERIAL_NUMBER",
        }
    ]


def test_append_rows_inserts_raw_rows_in_the_tab_range_and_returns_the_written_range() -> None:
    fake = FakeSheetsService(updated_range="'Gastos - Supermercado'!A20:I20")
    repository = GoogleSheetsRepository("SHEET_ID", service=fake)

    written = repository.append_rows([make_row(invoice_number="000123", purchase_date=date(2026, 10, 14), unit_price=0.94, total_price=1.88)])

    assert written == "'Gastos - Supermercado'!A20:I20"
    assert fake.calls == [
        {
            "method": "append",
            "spreadsheetId": "SHEET_ID",
            "range": "'Gastos - Supermercado'!A:I",
            "valueInputOption": "RAW",
            "insertDataOption": "INSERT_ROWS",
            "body": {"values": [["000123", 46309, "Lidl", "LLET", "Leche", "", "", 0.94, 1.88]]},
        }
    ]


def test_http_errors_become_actionable_sheets_errors() -> None:
    error = HttpError(httplib2.Response({"status": 403}), b"forbidden")
    repository = GoogleSheetsRepository("SHEET_ID", service=FakeSheetsService(error=error))
    with pytest.raises(SheetsError, match="Editor"):
        repository.read_rows()


def test_auth_errors_become_sheets_errors() -> None:
    repository = GoogleSheetsRepository("SHEET_ID", service=FakeSheetsService(error=RefreshError("boom")))
    with pytest.raises(SheetsError, match="serviceAccountTokenCreator"):
        repository.read_rows()

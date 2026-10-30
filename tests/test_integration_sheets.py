"""Prueba contra la COPIA DE PRUEBAS real de la hoja. No se ejecuta por defecto.

    uv run --env-file .env pytest -m integration -s

Lee TEST_SPREADSHEET_ID y nunca SPREADSHEET_ID, así que no puede tocar la hoja de
producción por error. Cada ejecución deja filas con el comercio "__PRUEBA__" en la
copia (el MCP no tiene herramienta de borrado): bórralas a mano cuando quieras.

Verifica contra Sheets lo que los tests unitarios no pueden (D19/D20):
- el nº de factura numérico con ceros se guarda como texto;
- la fecha se guarda como fecha real (con un día <= 12, ambiguo según el idioma);
- un nombre de ítem que parece fórmula se guarda como texto, sin evaluarse;
- las filas nuevas heredan el formato visual (fecha legible y precio en euros).
"""

import os
import time
from datetime import date
from typing import Any

import pytest

from gastos_mcp.models import ReceiptItem
from gastos_mcp.repository import GoogleSheetsRepository, date_to_serial
from gastos_mcp.service import DuplicateReceiptError, GastosService

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def spreadsheet_id() -> str:
    value = os.environ.get("TEST_SPREADSHEET_ID", "").strip()
    if not value:
        pytest.skip("Falta TEST_SPREADSHEET_ID (copia de pruebas de la hoja).")
    return value


def _get(repository: GoogleSheetsRepository, spreadsheet_id: str, cell_range: str, **options: str) -> list[list[Any]]:
    """Lee un rango con las opciones de renderizado dadas."""
    response = (
        repository._service.spreadsheets()
        .values()
        .get(  # noqa: SLF001 (acceso deliberado en el test)
            spreadsheetId=spreadsheet_id, range=cell_range, **options
        )
        .execute()
    )
    return response["values"]


def test_written_rows_keep_their_types_and_format(spreadsheet_id: str) -> None:
    repository = GoogleSheetsRepository(spreadsheet_id)
    service = GastosService(repository)
    invoice = f"000{int(time.time())}"  # numérica y con ceros a la izquierda, distinta en cada ejecución
    receipt_date = date(2026, 10, 5)
    items = [
        ReceiptItem(item_name="=1+1", category="__PRUEBA__", unit_price=0.01, total_price=0.03),
        ReceiptItem(item_name="+PROTEINA 100%", category="__PRUEBA__", unit_price=1.5, total_price=4.5),
    ]

    result = service.append_receipt(invoice, receipt_date, "__PRUEBA__", items)

    raw = _get(
        repository,
        spreadsheet_id,
        result.updated_range,
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="SERIAL_NUMBER",
    )
    shown = _get(repository, spreadsheet_id, result.updated_range, valueRenderOption="FORMATTED_VALUE")

    assert raw[0][0] == invoice, "el nº de factura numérico debe quedar como texto con sus ceros"
    assert raw[0][1] == date_to_serial(receipt_date), "la fecha debe ser un número de serie (fecha real)"
    assert raw[0][3] == "=1+1", "un nombre que parece fórmula debe guardarse como texto"
    assert raw[1][3] == "+PROTEINA 100%"
    assert raw[0][4] == "__PRUEBA__"
    assert raw[0][7] == 0.01 and raw[1][7] == 1.5, "el precio debe ser numérico"
    assert raw[0][8] == 0.03 and raw[1][8] == 4.5, "el precio total (columna I) debe ser numérico"
    assert shown[0][1] != str(date_to_serial(receipt_date)), "la fecha debe verse con formato de fecha, no como número"
    assert "€" in shown[0][7], "el precio debe verse con formato de euro"

    # El ticket recién escrito se detecta como duplicado y no se puede volver a escribir.
    assert service.check_receipt(invoice, "__PRUEBA__").exists is True
    with pytest.raises(DuplicateReceiptError):
        service.append_receipt(invoice, receipt_date, "__PRUEBA__", items)

"""Acceso a la pestaña "Gastos - Supermercado" de Google Sheets.

Define el contrato `SheetRepository` y dos implementaciones:
- `GoogleSheetsRepository`: la real (API de Sheets con ADC, sin archivos de clave).
- `InMemoryRepository`: en memoria, para tests y para el modo `--fake` del servidor.

Todo lo específico de Google (rangos, tipos de celda, números de serie de fecha)
queda aquí; el resto del código trabaja con `SheetRow`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from enum import IntEnum
from typing import Any, Protocol

import google.auth
from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gastos_mcp.models import SheetRow

logger = logging.getLogger(__name__)

# Alcance mínimo que permite escribir. El límite real lo dan los permisos de la
# hoja (Editor sobre el archivo) y el código: el rango siempre lleva el nombre de la pestaña.
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TAB_NAME = "Gastos - Supermercado"
HEADER_FIRST_CELL = "No. de factura"
# Día 0 de los números de serie de fecha de Sheets (30/12/1899).
SHEETS_EPOCH = date(1899, 12, 30)


class Column(IntEnum):
    """Columnas de la pestaña, en el orden en que aparecen en la hoja (A a I).

    Única fuente de verdad del esquema: el número de columnas, la letra de la última y el
    rango A1 se derivan de aquí, y el resto del código accede a las celdas por nombre y no
    por posición. Al insertar una columna se añade aquí con el valor que le corresponda
    (desplazando las siguientes): los valores deben ser 0, 1, 2... sin huecos.
    """

    INVOICE_NUMBER = 0
    PURCHASE_DATE = 1
    MERCHANT = 2
    ITEM_NAME = 3
    CATEGORY = 4
    PACK = 5
    DESCRIPTION = 6
    UNIT_PRICE = 7
    TOTAL_PRICE = 8

    @property
    def letter(self) -> str:
        """Letra de la columna en la hoja (0 -> "A"). Válido hasta la Z (26 columnas)."""
        return chr(ord("A") + self.value)


FIRST_COLUMN = min(Column)
LAST_COLUMN = max(Column)
COLUMN_COUNT = len(Column)
A1_RANGE = f"'{TAB_NAME}'!{FIRST_COLUMN.letter}:{LAST_COLUMN.letter}"


class SheetsError(Exception):
    """Fallo al acceder a la hoja, con un mensaje accionable."""


class SheetLayoutError(SheetsError):
    """La hoja no tiene la estructura esperada (A1 distinto de "No. de factura")."""


class SheetRepository(Protocol):
    """Contrato de acceso a la hoja."""

    def read_rows(self) -> list[SheetRow]:
        """Devuelve todas las filas de datos (sin la cabecera), con su número de fila."""
        ...

    def append_rows(self, rows: Sequence[SheetRow]) -> str:
        """Añade filas al final de la tabla y devuelve el rango escrito."""
        ...


# --- Conversión entre filas de la hoja y `SheetRow` (funciones puras) ----------------


def serial_to_date(serial: float) -> date:
    """Convierte un número de serie de fecha de Sheets en `date` (ignora la hora)."""
    return SHEETS_EPOCH + timedelta(days=int(serial))


def date_to_serial(value: date) -> int:
    """Convierte una `date` en número de serie de fecha de Sheets."""
    return (value - SHEETS_EPOCH).days


def _is_number(cell: Any) -> bool:
    """True para int/float (bool excluido: en Python es un int)."""
    return isinstance(cell, (int, float)) and not isinstance(cell, bool)


def _text(cell: Any) -> str:
    """Texto de una celda; un número entero guardado como número no lleva ".0"."""
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell).strip()


def _parse_date(cell: Any, row_number: int) -> date | None:
    """Fecha de una celda: número de serie, o texto DD/MM/YYYY o ISO. None si no se entiende."""
    if _is_number(cell):
        return serial_to_date(cell)
    text = str(cell).strip()
    if not text:
        return None
    for pattern in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    logger.warning("fila %d: fecha no interpretable %r", row_number, cell)
    return None


def _parse_number(cell: Any, row_number: int) -> float | None:
    """Número de una celda; tolera texto tipo "7,39 €". None si vacía o no se entiende."""
    if _is_number(cell):
        return float(cell)
    text = str(cell).strip().replace("€", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        logger.warning("fila %d: número no interpretable %r", row_number, cell)
        return None


def parse_sheet_values(values: list[list[Any]]) -> list[SheetRow]:
    """Convierte los valores crudos de la API (con la cabecera en la fila 1) en filas.

    Las filas completamente vacías se omiten, pero la numeración respeta su hueco.
    """
    header = values[0] if values else []
    if not header or str(header[Column.INVOICE_NUMBER]).strip().casefold() != HEADER_FIRST_CELL.casefold():
        raise SheetLayoutError(
            f"La celda {Column.INVOICE_NUMBER.letter}1 de '{TAB_NAME}' debe ser '{HEADER_FIRST_CELL}' "
            f"(estructura {FIRST_COLUMN.letter} a {LAST_COLUMN.letter}). "
            "Revisa que SPREADSHEET_ID apunte a la hoja correcta."
        )
    rows: list[SheetRow] = []
    for row_number, raw in enumerate(values[1:], start=2):
        cells = list(raw)[:COLUMN_COUNT]
        cells += [""] * (COLUMN_COUNT - len(cells))
        if not any(str(cell).strip() for cell in cells):
            continue
        rows.append(
            SheetRow(
                row_number=row_number,
                invoice_number=_text(cells[Column.INVOICE_NUMBER]),
                purchase_date=_parse_date(cells[Column.PURCHASE_DATE], row_number),
                merchant=_text(cells[Column.MERCHANT]),
                item_name=_text(cells[Column.ITEM_NAME]),
                category=_text(cells[Column.CATEGORY]),
                pack=_parse_number(cells[Column.PACK], row_number),
                description=_text(cells[Column.DESCRIPTION]),
                unit_price=_parse_number(cells[Column.UNIT_PRICE], row_number),
                total_price=_parse_number(cells[Column.TOTAL_PRICE], row_number),
            )
        )
    return rows


def row_to_cells(row: SheetRow) -> list[Any]:
    """Convierte una fila nueva en las celdas (una por `Column`) que se envían a la hoja.

    Se escribe con `RAW`, así que cada valor conserva el tipo JSON que tiene aquí:
    el nº de factura es texto (no pierde ceros), la fecha es un número de serie y
    los precios son números. Con `RAW` Sheets no interpreta nada (ni fechas según el
    idioma de la hoja ni fórmulas en un nombre de ítem que empiece por "=").
    Pack y descripción quedan vacíos: se rellenan manualmente.
    """
    if row.purchase_date is None or row.unit_price is None or row.total_price is None:
        raise ValueError("Una fila nueva necesita fecha, precio unitario y precio total.")
    # Se parte de celdas vacías y se rellena por nombre: el orden lo dicta `Column`,
    # no el orden en que se escriben estas líneas.
    cells: list[Any] = [""] * COLUMN_COUNT
    cells[Column.INVOICE_NUMBER] = row.invoice_number
    cells[Column.PURCHASE_DATE] = date_to_serial(row.purchase_date)
    cells[Column.MERCHANT] = row.merchant
    cells[Column.ITEM_NAME] = row.item_name
    cells[Column.CATEGORY] = row.category
    cells[Column.UNIT_PRICE] = row.unit_price
    cells[Column.TOTAL_PRICE] = row.total_price
    return cells


# --- Implementación real -----------------------------------------------------------


def explain_http_error(error: HttpError) -> str:
    """Traduce los errores HTTP más probables de la API de Sheets a una causa concreta."""
    hints = {
        400: f"Rango inválido: ¿existe la pestaña '{TAB_NAME}' con ese nombre exacto?",
        403: "Sin permiso: ¿la hoja está compartida como Editor con la cuenta de servicio "
        "y la API de Sheets está habilitada en su proyecto?",
        404: "No se encuentra la hoja: revisa SPREADSHEET_ID.",
        429: "Cuota por minuto superada: espera un minuto y reintenta.",
    }
    return hints.get(error.status_code, "Error no previsto de la API de Sheets.") + f" Detalle: {error}"


class GoogleSheetsRepository:
    """Repositorio sobre la API de Google Sheets.

    Autenticación: Application Default Credentials. En local, suplantación de la
    cuenta de servicio (`gcloud auth application-default login --impersonate-service-account`);
    en Cloud Run, la cuenta de servicio del servicio. El mismo código vale para ambos.

    El cliente de Google (httplib2) no es thread-safe: el servicio que lo usa
    serializa las llamadas con un candado.
    """

    def __init__(self, spreadsheet_id: str, service: Any | None = None) -> None:
        """`service` solo se inyecta en tests; en producción se crea con ADC."""
        self._spreadsheet_id = spreadsheet_id
        self._service = service if service is not None else self._build_service()

    @staticmethod
    def _build_service() -> Any:
        """Crea el cliente de la API de Sheets con las credenciales por defecto (ADC)."""
        try:
            credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
        except DefaultCredentialsError as error:
            raise SheetsError(
                "No hay credenciales ADC. Ejecuta: gcloud auth application-default login "
                "--impersonate-service-account=<SA_EMAIL>"
            ) from error
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def read_rows(self) -> list[SheetRow]:
        """Lee la tabla (`A1_RANGE`) con valores reales: fechas como número de serie y precios como número."""
        request = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=A1_RANGE,
                valueRenderOption="UNFORMATTED_VALUE",
                dateTimeRenderOption="SERIAL_NUMBER",
            )
        )
        response = self._execute(request)
        return parse_sheet_values(response.get("values", []))

    def append_rows(self, rows: Sequence[SheetRow]) -> str:
        """Añade las filas justo después de la última de la tabla."""
        request = (
            self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self._spreadsheet_id,
                range=A1_RANGE,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row_to_cells(row) for row in rows]},
            )
        )
        response = self._execute(request)
        return str(response["updates"]["updatedRange"])

    @staticmethod
    def _execute(request: Any) -> dict[str, Any]:
        """Ejecuta una petición traduciendo los errores de Google a `SheetsError`."""
        try:
            return request.execute()
        except HttpError as error:
            raise SheetsError(explain_http_error(error)) from error
        except GoogleAuthError as error:
            raise SheetsError(
                "Falló la obtención del token. Revisa el rol roles/iam.serviceAccountTokenCreator "
                "sobre la cuenta de servicio, la API iamcredentials y repite el login ADC. "
                f"Detalle: {error}"
            ) from error


# --- Implementación en memoria (tests y modo --fake) ---------------------------------


class InMemoryRepository:
    """Repositorio en memoria con el mismo contrato que `GoogleSheetsRepository`."""

    def __init__(self, rows: Iterable[SheetRow] = ()) -> None:
        self._rows: list[SheetRow] = []
        self.append_rows(list(rows))

    def read_rows(self) -> list[SheetRow]:
        """Devuelve copias, para que quien llama no modifique el estado interno."""
        return [row.model_copy() for row in self._rows]

    def append_rows(self, rows: Sequence[SheetRow]) -> str:
        """Añade filas numeradas como en la hoja (la fila 1 es la cabecera)."""
        first = len(self._rows) + 2
        for offset, row in enumerate(rows):
            self._rows.append(row.model_copy(update={"row_number": first + offset}))
        last = first + max(len(rows), 1) - 1
        return f"'{TAB_NAME}'!{FIRST_COLUMN.letter}{first}:{LAST_COLUMN.letter}{last}"


def demo_rows() -> list[SheetRow]:
    """Datos de ejemplo para `--fake` (inspirados en las filas del contexto del proyecto)."""
    return [
        SheetRow(
            invoice_number="",
            purchase_date=date(2026, 9, 30),
            merchant="bonArea",
            item_name="FARINA BLAT BONAREA 1 K",
            category="Harina",
            pack=1,
            description="Harina de trigo - 1 Kg",
            unit_price=0.71,
            total_price=0.71,
        ),
        SheetRow(
            invoice_number="L-1001",
            purchase_date=date(2026, 10, 5),
            merchant="Lidl",
            item_name="SENSE LACTOSA SEMIDE",
            category="Leche",
            pack=1,
            description="Leche semidescremada sin lactosa - 1 L",
            unit_price=0.94,
            total_price=1.88,
        ),
        SheetRow(
            invoice_number="B-2002",
            purchase_date=date(2026, 10, 7),
            merchant="bonÀrea",
            item_name="MANTEGA SENSE SAL 250G",
            category="Mantequilla",
            unit_price=2.45,
            total_price=2.45,
        ),
        SheetRow(
            invoice_number="L-1003",
            purchase_date=date(2026, 10, 12),
            merchant="Lidl",
            item_name="MANTEGA 250G",
            category="Mantequilla",
            unit_price=2.19,
            total_price=2.19,
        ),
    ]

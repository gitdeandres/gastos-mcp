"""Utilidades compartidas por los tests."""

from datetime import date
from typing import Any

from gastos_mcp.models import SheetRow


def make_row(**overrides: Any) -> SheetRow:
    """Fila de ejemplo; cada test sobrescribe solo lo que le interesa."""
    defaults: dict[str, Any] = {
        "invoice_number": "A-1",
        "purchase_date": date(2026, 10, 1),
        "merchant": "Lidl",
        "item_name": "LLET",
        "category": "Leche",
        "unit_price": 1.0,
        "total_price": 1.0,
    }
    return SheetRow(**{**defaults, **overrides})


class _FakeRequest:
    """Petición simulada: `execute()` devuelve la respuesta o lanza el error configurado."""

    def __init__(self, response: dict[str, Any], error: Exception | None) -> None:
        self._response = response
        self._error = error

    def execute(self) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return self._response


class FakeSheetsService:
    """Imita la cadena `.spreadsheets().values().get/append(...).execute()` del cliente de Google.

    Registra cada llamada en `calls` para comprobar los parámetros exactos que se envían a la API.
    """

    def __init__(
        self,
        values: list[list[Any]] | None = None,
        updated_range: str = "'Gastos - Supermercado'!A5:I6",
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._values = values or []
        self._updated_range = updated_range
        self._error = error

    def spreadsheets(self) -> "FakeSheetsService":
        return self

    def values(self) -> "FakeSheetsService":
        return self

    def get(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append({"method": "get", **kwargs})
        return _FakeRequest({"values": self._values}, self._error)

    def append(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append({"method": "append", **kwargs})
        return _FakeRequest({"updates": {"updatedRange": self._updated_range}}, self._error)

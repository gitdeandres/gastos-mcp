"""Modelos de datos del MCP de Gastos.

Una fila de la hoja es un ítem de un ticket. Los mismos modelos validan lo que
Claude envía y describen lo que el servidor devuelve.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

MAX_ITEMS_PER_RECEIPT = 100
# Filas que devuelve search_prices si no se indica `limit` (lo usan el servicio y la herramienta).
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50

# Tipos reutilizados en la firma de las herramientas y en el servicio: así la
# validación vive en un solo sitio.
InvoiceNumber = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=60),
    Field(description="Número de factura/ticket tal como figura, como texto (conserva ceros a la izquierda)."),
]
Merchant = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    Field(description="Comercio tal como figura en el ticket."),
]


class ReceiptItem(BaseModel):
    """Un ítem del ticket tal como se escribirá en la hoja."""

    item_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
        Field(description="Nombre del ítem transcrito textualmente del ticket, sin normalizar ni añadir cantidades."),
    ]
    category: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=60),
        Field(description="Categoría genérica en español (Leche, Huevos...). Reutiliza las de list_categories."),
    ]
    unit_price: Annotated[
        float,
        Field(ge=0, le=10000, description="Precio unitario pagado (descuento aplicado), sin símbolo de moneda."),
    ]

    @field_validator("unit_price")
    @classmethod
    def _at_most_two_decimals(cls, value: float) -> float:
        """Los tickets están en euros: más de dos decimales indica un error de lectura."""
        if abs(value * 100 - round(value * 100)) > 1e-6:
            raise ValueError("el precio debe tener como máximo 2 decimales")
        return value


class SheetRow(BaseModel):
    """Una fila de la hoja (columnas A a H)."""

    row_number: int | None = Field(default=None, description="Número de fila en la hoja (solo en filas leídas).")
    invoice_number: str = Field(default="", description="Vacío en los registros anteriores a la estructura nueva.")
    purchase_date: date | None = Field(default=None, description="None si la celda no tiene una fecha interpretable.")
    merchant: str = ""
    item_name: str = ""
    category: str = Field(default="", description="Vacía si todavía no se ha clasificado.")
    pack: float | None = Field(default=None, description="Columna F: unidades por paquete.")
    description: str = Field(default="", description="Columna G: descripción.")
    unit_price: float | None = Field(default=None, description="Precio unitario en euros.")


class ReceiptMatch(BaseModel):
    """Ticket ya registrado que coincide por número de factura y comercio."""

    purchase_date: date | None
    merchant: str
    item_count: int
    first_row: int | None


class CheckResult(BaseModel):
    """Resultado de comprobar si un ticket ya está registrado."""

    exists: bool
    matches: list[ReceiptMatch]


class SearchResult(BaseModel):
    """Resultado de una búsqueda de precios, de más reciente a más antiguo."""

    total_matches: int = Field(
        description="Coincidencias totales (con latest_per_merchant, comercios distintos); "
        "si es mayor que las filas devueltas, se truncó."
    )
    rows: list[SheetRow]


class CategoryCount(BaseModel):
    """Categoría existente y número de filas que la usan."""

    name: str
    count: int


class AppendResult(BaseModel):
    """Resultado de escribir un ticket."""

    rows_written: int
    updated_range: str
    new_categories: list[str] = Field(description="Categorías que no existían antes de esta escritura.")
    rows: list[SheetRow] = Field(description="Filas escritas, con la categoría final aplicada.")

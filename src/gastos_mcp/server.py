"""Servidor MCP de Gastos: expone cuatro herramientas sobre `GastosService`.

Transporte Streamable HTTP sin estado en /mcp, protegido con un token
por cabecera (D12, módulo `auth`). Este módulo solo traduce entre MCP y el servicio:
la lógica vive en `service.py`.
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date
from typing import Annotated, Any, TypeVar

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.types import ASGIApp

from gastos_mcp.auth import AuthConfigError, BearerTokenMiddleware, load_token
from gastos_mcp.models import (
    DEFAULT_SEARCH_LIMIT,
    MAX_ITEMS_PER_RECEIPT,
    MAX_SEARCH_LIMIT,
    AppendResult,
    CategoryCount,
    CheckResult,
    InvoiceNumber,
    Merchant,
    ReceiptItem,
    SearchResult,
)
from gastos_mcp.repository import GoogleSheetsRepository, InMemoryRepository, SheetsError, demo_rows
from gastos_mcp.service import DuplicateReceiptError, GastosService

F = TypeVar("F", bound=Callable[..., Any])

INSTRUCTIONS = (
    "Registro de precios de supermercado. Cada fila de la hoja es un ítem de un ticket; "
    "sirve para comparar el precio de cada artículo entre comercios."
)


@contextmanager
def _as_tool_errors() -> Generator[None]:
    """Convierte los errores esperados en errores de herramienta con mensaje legible para Claude."""
    try:
        yield
    except (SheetsError, DuplicateReceiptError, ValueError) as error:
        raise ToolError(str(error)) from error


def create_server(service: GastosService) -> MCPServer:
    """Crea el servidor MCP con sus herramientas conectadas al servicio dado."""
    mcp = MCPServer(name="gastos", instructions=INSTRUCTIONS)

    def register(title: str, annotations: ToolAnnotations) -> Callable[[F], F]:
        """Registra la función como herramienta usando su docstring como descripción.

        El SDK copia el docstring tal cual, con saltos de línea y sangría; aquí se
        compacta para que Claude reciba un texto limpio.
        """

        def decorator(function: F) -> F:
            description = " ".join((function.__doc__ or "").split())
            return mcp.tool(title=title, description=description, annotations=annotations)(function)

        return decorator

    # Las lecturas se marcan readOnlyHint=True y la escritura destructiveHint=True: Claude usa
    # estas anotaciones para decidir qué herramientas pueden ejecutarse sin confirmación y
    # cuáles piden aprobación en cada llamada. Son pistas para el cliente, no una garantía
    # del servidor; el control real es el permiso "Needs approval" del conector (D8).
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

    @register(title="Comprobar si un ticket ya está registrado", annotations=read_only)
    def check_receipt(invoice_number: InvoiceNumber, merchant: Merchant) -> CheckResult:
        """Comprueba si un ticket ya está en la hoja, cruzando número de factura y comercio
        (sin mirar la fecha). Devuelve las coincidencias con su fecha, para distinguir un
        duplicado real de una coincidencia de numeración. No escribe nada."""
        with _as_tool_errors():
            return service.check_receipt(invoice_number, merchant)

    @register(title="Escribir los ítems de un ticket en la hoja", annotations=write)
    def append_receipt_items(
        invoice_number: InvoiceNumber,
        purchase_date: Annotated[date, Field(description="Fecha del ticket en formato AAAA-MM-DD.")],
        merchant: Merchant,
        items: Annotated[
            list[ReceiptItem],
            Field(min_length=1, max_length=MAX_ITEMS_PER_RECEIPT, description="Una entrada por línea del ticket."),
        ],
        allow_existing_invoice: Annotated[
            bool,
            Field(description="Solo true si el usuario confirmó que es otro ticket con la misma numeración."),
        ] = False,
    ) -> AppendResult:
        """Añade al final de la hoja las filas de un ticket, una por ítem (columnas A, B, C, D, E,
        H e I; F y G quedan vacías). unit_price es el precio unitario y total_price el importe de
        la línea tal como lo imprime el ticket, con el descuento de línea ya aplicado; el servidor
        no lo calcula ni lo comprueba. Productos al peso: unitario = precio por kg, total = lo
        cobrado. "2x 2.55": una sola fila con unitario 2.55 y total 5.10. Si el ticket no deja
        claro el total de una línea, pregunta al usuario; no lo adivines. Rechaza el ticket si ya
        existe la misma factura del mismo comercio. Escritura que este servidor no puede deshacer
        (no hay herramienta para editar ni borrar): debe invocarse solo tras la aprobación
        explícita del usuario sobre los datos."""
        with _as_tool_errors():
            return service.append_receipt(invoice_number, purchase_date, merchant, items, allow_existing_invoice)

    @register(title="Buscar historial de precios", annotations=read_only)
    def search_prices(
        query: Annotated[
            str | None,
            Field(description="Texto a buscar en nombre del ticket, categoría y descripción (todas las palabras)."),
        ] = None,
        category: Annotated[
            str | None, Field(description="Categoría exacta (ver list_categories); ignora mayúsculas y acentos.")
        ] = None,
        merchant: Annotated[str | None, Field(description="Comercio; ignora mayúsculas y acentos.")] = None,
        limit: Annotated[
            int, Field(ge=1, le=MAX_SEARCH_LIMIT, description="Máximo de filas a devolver.")
        ] = DEFAULT_SEARCH_LIMIT,
        latest_per_merchant: Annotated[
            bool,
            Field(description="true para devolver solo la compra más reciente de cada comercio (comparar precios)."),
        ] = False,
    ) -> SearchResult:
        """Busca en el historial de precios, de más reciente a más antiguo. Hace falta indicar
        category o query; merchant es un filtro opcional. Para comparar dónde conviene comprar
        algo, usa latest_per_merchant=true: devuelve el último precio de cada comercio. Devuelve
        como máximo `limit` filas y el total de coincidencias."""
        with _as_tool_errors():
            return service.search_prices(query, category, merchant, limit, latest_per_merchant)

    @register(title="Listar categorías existentes", annotations=read_only)
    def list_categories() -> list[CategoryCount]:
        """Lista las categorías ya usadas en la columna Clasificación, con el número de filas de
        cada una, para reutilizarlas al clasificar."""
        with _as_tool_errors():
            return service.list_categories()

    return mcp


def build_app(service: GastosService, token: str, host: str = "127.0.0.1") -> ASGIApp:
    """App ASGI completa: la app Streamable HTTP del SDK envuelta por la autenticación.

    `host` solo se pasa al SDK para que active su protección contra DNS rebinding cuando
    se escucha en localhost; en el contenedor (0.0.0.0) la protección es el token.
    """
    app = create_server(service).streamable_http_app(
        # stateless_http: cada petición es independiente (sirve con min-instances=0 en Cloud Run).
        # json_response: respuestas JSON simples en lugar de flujos SSE.
        stateless_http=True,
        json_response=True,
        host=host,
    )
    return BearerTokenMiddleware(app, token)


def main(argv: list[str] | None = None) -> None:
    """Arranca el servidor (por defecto en http://127.0.0.1:8000/mcp)."""
    parser = argparse.ArgumentParser(description="Servidor MCP de Gastos")
    parser.add_argument("--fake", action="store_true", help="usa datos de ejemplo en memoria, sin tocar Google Sheets")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # El token se valida antes que nada: sin token el servidor no debe llegar a escuchar.
    try:
        token = load_token()
    except AuthConfigError as error:
        sys.exit(str(error))

    if args.fake:
        repository: GoogleSheetsRepository | InMemoryRepository = InMemoryRepository(demo_rows())
        logging.getLogger(__name__).warning("Modo --fake: datos de ejemplo en memoria, no se usa Google Sheets.")
    else:
        spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
        if not spreadsheet_id:
            sys.exit("Falta SPREADSHEET_ID (ver .env.example).")
        try:
            repository = GoogleSheetsRepository(spreadsheet_id)
        except SheetsError as error:
            sys.exit(str(error))

    app = build_app(GastosService(repository), token, host=args.host)
    # Se arranca uvicorn directamente (y no `MCPServer.run`) porque `run` construye su
    # propia app y no permite envolverla con el middleware de autenticación.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

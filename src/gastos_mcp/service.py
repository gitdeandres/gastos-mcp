"""Lógica de negocio del MCP de Gastos.

No depende de MCP ni de Google: recibe un `SheetRepository`. Eso permite probar
todas las reglas con un repositorio en memoria.
"""

from __future__ import annotations

import threading
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date

from gastos_mcp.models import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    AppendResult,
    CategoryCount,
    CheckResult,
    ReceiptItem,
    ReceiptMatch,
    SearchResult,
    SheetRow,
)
from gastos_mcp.repository import SheetRepository


class DuplicateReceiptError(Exception):
    """Ya existe un ticket con el mismo número de factura y comercio."""

    def __init__(self, invoice_number: str, existing: CheckResult) -> None:
        self.existing = existing
        detail = "; ".join(
            f"{match.purchase_date.strftime('%d/%m/%Y') if match.purchase_date else 'fecha desconocida'} "
            f"({match.item_count} ítems, fila {match.first_row})"
            for match in existing.matches
        )
        super().__init__(
            f"Ya hay registrado un ticket con la factura '{invoice_number}' del mismo comercio: {detail}. "
            "No se ha escrito nada. Si es un ticket distinto con la misma numeración, "
            "confírmalo con el usuario y repite con allow_existing_invoice=true."
        )


def normalize(text: str) -> str:
    """Minúsculas, sin acentos y con espacios colapsados: "bonÀrea" -> "bonarea"."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.casefold().split())


def _invoice_key(invoice_number: str) -> str:
    """Clave de comparación de facturas: ignora mayúsculas y ceros a la izquierda.

    Una factura guardada alguna vez como número habría perdido sus ceros ("000123" -> "123");
    así se sigue reconociendo como la misma.
    """
    return normalize(invoice_number).lstrip("0") or "0"


def _merchants_match(first: str, second: str) -> bool:
    """Dos comercios coinciden si uno contiene al otro ("Lidl" y "LIDL SUPERMERCADOS SAU")."""
    a, b = normalize(first), normalize(second)
    return bool(a and b and (a in b or b in a))


def _find_receipt(rows: Sequence[SheetRow], invoice_number: str, merchant: str) -> CheckResult:
    """Busca los ítems con el mismo número de factura y comercio (sin mirar la fecha).

    Se agrupan por fecha: una misma factura con fechas distintas puede ser una
    coincidencia de numeración y no un duplicado, y la decide el usuario.
    """
    key = _invoice_key(invoice_number)
    groups: dict[date | None, list[SheetRow]] = defaultdict(list)
    for row in rows:
        if row.invoice_number and _invoice_key(row.invoice_number) == key and _merchants_match(row.merchant, merchant):
            groups[row.purchase_date].append(row)
    matches = [
        ReceiptMatch(
            purchase_date=purchase_date,
            merchant=group[0].merchant,
            item_count=len(group),
            first_row=group[0].row_number,
        )
        for purchase_date, group in groups.items()
    ]
    matches.sort(key=lambda match: match.purchase_date or date.min, reverse=True)
    return CheckResult(exists=bool(matches), matches=matches)


def _category_spellings(rows: Sequence[SheetRow]) -> dict[str, Counter[str]]:
    """Agrupa las categorías por clave normalizada, contando cada grafía usada."""
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        if row.category:
            spellings[normalize(row.category)][row.category] += 1
    return spellings


def _latest_per_merchant(rows: Sequence[SheetRow]) -> list[SheetRow]:
    """Deja la primera fila de cada comercio en una lista ya ordenada de más reciente a más antigua.

    Agrupa por nombre normalizado ("bonArea" = "bonÀrea"), no por contención como en los
    duplicados: aquí agrupar "Lidl" con "Lidl Express" mezclaría dos tiendas distintas.
    """
    seen: set[str] = set()
    latest: list[SheetRow] = []
    for row in rows:
        key = normalize(row.merchant)
        if key not in seen:
            seen.add(key)
            latest.append(row)
    return latest


class GastosService:
    """Operaciones del MCP sobre la hoja."""

    def __init__(self, repository: SheetRepository) -> None:
        self._repository = repository
        # Un solo hilo a la vez. Resuelve dos problemas concretos: (1) el SDK ejecuta las
        # herramientas síncronas en hilos y el cliente de Google no es thread-safe; (2) la
        # comprobación de duplicado y la escritura deben ser atómicas, o dos llamadas
        # simultáneas con el mismo ticket podrían escribirlo dos veces.
        self._lock = threading.Lock()

    def check_receipt(self, invoice_number: str, merchant: str) -> CheckResult:
        """Indica si ya hay un ticket con ese número de factura y comercio."""
        with self._lock:
            rows = self._repository.read_rows()
        return _find_receipt(rows, invoice_number, merchant)

    def append_receipt(
        self,
        invoice_number: str,
        purchase_date: date,
        merchant: str,
        items: Sequence[ReceiptItem],
        allow_existing_invoice: bool = False,
    ) -> AppendResult:
        """Escribe un ticket (una fila por ítem), rechazando duplicados.

        Cada categoría se ajusta a la grafía ya usada en la hoja si solo difiere en
        mayúsculas o acentos, para no fragmentarlas ("leche" -> "Leche").
        """
        with self._lock:
            rows = self._repository.read_rows()
            if not allow_existing_invoice:
                existing = _find_receipt(rows, invoice_number, merchant)
                if existing.exists:
                    raise DuplicateReceiptError(invoice_number, existing)

            canonical = {key: counter.most_common(1)[0][0] for key, counter in _category_spellings(rows).items()}
            new_categories: list[str] = []
            new_rows: list[SheetRow] = []
            for item in items:
                key = normalize(item.category)
                if key not in canonical:
                    canonical[key] = item.category
                    new_categories.append(item.category)
                new_rows.append(
                    SheetRow(
                        invoice_number=invoice_number,
                        purchase_date=purchase_date,
                        merchant=merchant,
                        item_name=item.item_name,
                        category=canonical[key],
                        unit_price=item.unit_price,
                        total_price=item.total_price,
                    )
                )
            updated_range = self._repository.append_rows(new_rows)
        return AppendResult(
            rows_written=len(new_rows),
            updated_range=updated_range,
            new_categories=new_categories,
            rows=new_rows,
        )

    def search_prices(
        self,
        query: str | None = None,
        category: str | None = None,
        merchant: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        latest_per_merchant: bool = False,
    ) -> SearchResult:
        """Busca filas por categoría exacta y/o texto, de más reciente a más antigua.

        El filtrado se hace aquí y no en Claude para devolver solo `limit` filas
        (el historial crecerá y no merece ocupar la conversación). Con
        `latest_per_merchant` devuelve solo la compra más reciente de cada comercio:
        así varias compras recientes en un comercio no tapan el precio de otro.
        """
        if not (query and query.strip()) and not (category and category.strip()):
            raise ValueError("Indica al menos 'query' o 'category'.")
        with self._lock:
            rows = self._repository.read_rows()

        tokens = normalize(query).split() if query else []
        category_key = normalize(category) if category else ""
        matches: list[SheetRow] = []
        for row in rows:
            if category_key and normalize(row.category) != category_key:
                continue
            if merchant and not _merchants_match(row.merchant, merchant):
                continue
            if tokens:
                haystack = normalize(" ".join((row.item_name, row.category, row.description)))
                if not all(token in haystack for token in tokens):
                    continue
            matches.append(row)
        # Las filas sin fecha interpretable van al final.
        matches.sort(key=lambda row: (row.purchase_date or date.min, row.row_number or 0), reverse=True)
        if latest_per_merchant:
            matches = _latest_per_merchant(matches)
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        return SearchResult(total_matches=len(matches), rows=matches[:limit])

    def list_categories(self) -> list[CategoryCount]:
        """Categorías únicas de la columna E (sin distinguir mayúsculas ni acentos), con su uso."""
        with self._lock:
            rows = self._repository.read_rows()
        spellings = _category_spellings(rows)
        return [
            CategoryCount(name=counter.most_common(1)[0][0], count=sum(counter.values()))
            for _, counter in sorted(spellings.items())
        ]

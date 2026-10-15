"""Tests de las herramientas a través del protocolo MCP (cliente y servidor en el mismo proceso)."""

from collections.abc import Sequence
from typing import Any

import pytest
from helpers import make_row
from mcp import Client

from gastos_mcp.models import SheetRow
from gastos_mcp.repository import InMemoryRepository, SheetLayoutError
from gastos_mcp.server import create_server
from gastos_mcp.service import GastosService

pytestmark = pytest.mark.anyio

VALID_RECEIPT: dict[str, Any] = {
    "invoice_number": "000123",
    "purchase_date": "2026-10-14",
    "merchant": "Lidl",
    "items": [{"item_name": "SENSE LACTOSA SEMIDE", "category": "leche", "unit_price": 0.94}],
}


def make_client(*rows: SheetRow) -> tuple[Client, InMemoryRepository]:
    repository = InMemoryRepository(rows)
    return Client(create_server(GastosService(repository))), repository


async def test_exposes_the_four_tools_with_the_right_annotations() -> None:
    client, _ = make_client()
    async with client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == {"check_receipt", "append_receipt_items", "search_prices", "list_categories"}
    for name in ("check_receipt", "search_prices", "list_categories"):
        assert tools[name].annotations is not None and tools[name].annotations.read_only_hint is True
    write = tools["append_receipt_items"].annotations
    assert write is not None and write.read_only_hint is False and write.destructive_hint is True
    assert all(tool.title for tool in tools.values())


async def test_append_then_read_back_through_the_tools() -> None:
    client, repository = make_client(make_row(category="Leche", invoice_number="OLD"))
    async with client:
        written = await client.call_tool("append_receipt_items", VALID_RECEIPT)
        found = await client.call_tool("check_receipt", {"invoice_number": "123", "merchant": "lidl"})
        searched = await client.call_tool("search_prices", {"category": "Leche", "limit": 1})

    assert written.is_error is False
    assert written.structured_content is not None
    assert written.structured_content["rows_written"] == 1
    assert written.structured_content["new_categories"] == []  # "leche" se ajustó a "Leche"
    assert found.structured_content is not None and found.structured_content["exists"] is True
    assert searched.structured_content is not None
    assert searched.structured_content["rows"][0]["purchase_date"] == "2026-10-14"
    assert len(repository.read_rows()) == 2


async def test_search_latest_per_merchant_through_the_tool() -> None:
    client, _ = make_client(
        make_row(merchant="Lidl", item_name="L-OLD"),
        make_row(merchant="Lidl", item_name="L-NEW", purchase_date=make_row().purchase_date.replace(day=9)),  # type: ignore[union-attr]
        make_row(merchant="bonÀrea", item_name="B"),
    )
    async with client:
        result = await client.call_tool("search_prices", {"category": "Leche", "latest_per_merchant": True})
    assert result.structured_content is not None
    assert [row["item_name"] for row in result.structured_content["rows"]] == ["L-NEW", "B"]


async def test_duplicate_is_an_error_and_nothing_is_written() -> None:
    client, repository = make_client()
    async with client:
        await client.call_tool("append_receipt_items", VALID_RECEIPT)
        second = await client.call_tool("append_receipt_items", VALID_RECEIPT)

    assert second.is_error is True
    assert "No se ha escrito nada" in second.content[0].text  # type: ignore[union-attr]
    assert len(repository.read_rows()) == 1


async def test_list_categories_returns_the_existing_ones() -> None:
    client, _ = make_client(make_row(category="Leche"), make_row(category="Huevos"))
    async with client:
        result = await client.call_tool("list_categories", {})
    assert result.structured_content == {"result": [{"name": "Huevos", "count": 1}, {"name": "Leche", "count": 1}]}


@pytest.mark.parametrize(
    "change",
    [
        {"items": []},
        {"items": [{"item_name": "X", "category": "Leche", "unit_price": -1}]},
        {"items": [{"item_name": "X", "category": "Leche", "unit_price": 1.234}]},
        {"items": [{"item_name": "", "category": "Leche", "unit_price": 1}]},
        {"purchase_date": "14/10/2026"},
        {"invoice_number": "   "},
    ],
)
async def test_invalid_input_is_rejected_without_writing(change: dict[str, Any]) -> None:
    client, repository = make_client()
    async with client:
        result = await client.call_tool("append_receipt_items", {**VALID_RECEIPT, **change})
    assert result.is_error is True
    assert repository.read_rows() == []


async def test_search_without_query_or_category_is_an_error() -> None:
    client, _ = make_client()
    async with client:
        result = await client.call_tool("search_prices", {"merchant": "Lidl"})
    assert result.is_error is True


class BrokenLayoutRepository:
    """Repositorio cuya hoja no tiene la estructura esperada."""

    def read_rows(self) -> list[SheetRow]:
        raise SheetLayoutError("La celda A1 debe ser 'No. de factura'")

    def append_rows(self, rows: Sequence[SheetRow]) -> str:
        raise AssertionError("no debe escribirse")


async def test_sheet_errors_reach_claude_as_readable_tool_errors() -> None:
    client = Client(create_server(GastosService(BrokenLayoutRepository())))
    async with client:
        result = await client.call_tool("list_categories", {})
    assert result.is_error is True
    assert "No. de factura" in result.content[0].text  # type: ignore[union-attr]

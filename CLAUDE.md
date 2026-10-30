# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Servidor MCP (Python 3.13, `uv`) que registra y consulta precios de supermercado en una pestaña de Google Sheets. Se despliega en Cloud Run y se usa desde Claude como conector remoto. La documentación del proyecto está en español; mantén ese idioma en comentarios, docstrings y mensajes.

## Comandos

```bash
uv sync                                            # dependencias + comando gastos-mcp
uv run pytest                                      # unitarios (la integración está excluida por defecto)
uv run pytest tests/test_service.py::test_nombre   # una sola prueba
uv run ruff check                                  # lint (CI ejecuta pytest y ruff)
uv run --env-file .env pytest -m integration -s    # contra una COPIA de pruebas real (ADC + TEST_SPREADSHEET_ID)
uv run --env-file .env gastos-mcp --fake           # servidor con datos en memoria, sin Google
uv run --env-file .env gastos-mcp                  # servidor contra SPREADSHEET_ID
```

El servidor escucha en `http://127.0.0.1:8000/mcp` (Streamable HTTP sin estado). `MCP_AUTH_TOKEN` es obligatorio incluso con `--fake`. En local usa siempre una copia de la hoja, nunca la de producción. Ruff: línea de 120, reglas `E,F,I,UP,B`.

## Arquitectura

Capas en `src/gastos_mcp/`, de abajo arriba:

- `repository.py`: contrato `SheetRepository` con dos implementaciones, `GoogleSheetsRepository` (API de Sheets con ADC, sin claves) e `InMemoryRepository` (tests y `--fake`). Todo lo específico de Google (rangos, números de serie de fecha, modo `RAW`) vive aquí. El enum `Column` es la única fuente de verdad del esquema: de él salen el nº de columnas, el rango A1 y las letras.
- `service.py`: reglas de negocio (duplicados por nº de factura + comercio, categorías, búsqueda) y el candado que serializa detección de duplicados y escritura.
- `models.py`: modelos Pydantic que validan la entrada del cliente y describen las respuestas.
- `auth.py`: middleware ASGI de token Bearer (comparación en tiempo constante, 401 vacío en cualquier ruta, valida el token al arrancar).
- `server.py`: las 4 herramientas MCP (`check_receipt`, `append_receipt_items`, `search_prices`, `list_categories`), `build_app` y el arranque con uvicorn.

Invariantes de diseño: solo se añaden filas (no hay editar ni borrar); la escritura usa `RAW` para que nada se interprete como fórmula o fecha; el servidor escribe las columnas A–E, H e I, y F (Pack) y G (Descripción) se rellenan a mano.

## Cambiar el esquema de la hoja

1. Edita `Column` en `repository.py` (valores 0, 1, 2… sin huecos).
2. Si la columna se lee o escribe, añade su línea en `parse_sheet_values` y `row_to_cells`.
3. Actualiza a mano los literales de `tests/test_repository.py` y `tests/test_schema.py`: son el contrato con la hoja real y a propósito no importan las constantes del código.
4. Ejecuta unitarios e integración contra la copia de pruebas.

## Docker / despliegue

`.dockerignore` es una lista de permitidos (solo `pyproject.toml`, `uv.lock`, `.python-version`, `src/`); el build usa `uv sync --locked`, así que `uv.lock` debe estar commiteado y `pyproject.toml` no debe declarar `readme` ni `license-files`, porque esos ficheros no existen en la imagen. El procedimiento de Cloud Run está en `docs/despliegue-cloud-run.md`.

## Reglas del proyecto (no deducibles del código)

### Idioma y estilo
- Código (variables, funciones, clases, módulos) en inglés. Comentarios y docstrings en español.
- Documenta con un docstring breve toda función y clase nueva; comenta solo lo no evidente.
- Complejidad proporcional: no añadas capas, abstracciones ni patrones que no resuelvan un problema
  concreto de hoy. Si usas uno, di en el PR qué problema resuelve.
- Línea máxima 120 (ruff). Python 3.13, gestor `uv`. No añadas dependencias sin pedirlo.

### Flujo de trabajo
- Trabaja siempre en una rama; `main` está protegido (PR obligatorio, check `ci`, solo squash).
- Commits convencionales: tipo en inglés, descripción en español (`feat: ...`, `test: ...`, `docs: ...`).
  Commits pequeños y separados por tema.
- No empujes sin que lo pida. No subas la `version` de `pyproject.toml` ni crees etiquetas: eso se hace
  solo al desplegar y lo hace Andrés.
- Antes de dar algo por hecho: `uv run pytest` y `uv run ruff check` deben pasar.
- NO despliegues ni ejecutes `gcloud` ni `docker`. El despliegue es manual y lo hace Andrés.

### Seguridad (el repositorio es PÚBLICO)
- Nunca escribas en código, tests, docs ni mensajes de commit: IDs de hojas de cálculo, ID del proyecto GCP,
  URLs reales del servicio, correos, tokens ni rutas personales. Usa placeholders.
- No leas `.env`. Las pruebas de integración (`-m integration`) solo se ejecutan si Andrés lo pide
  y solo contra la hoja de pruebas (`TEST_SPREADSHEET_ID`), nunca contra producción.

### Invariantes del diseño (no los cambies sin avisar)
- `Column(IntEnum)` en `repository.py` es la única fuente del esquema de columnas; `COLUMN_COUNT` y
  `A1_RANGE` se derivan de él. Al añadir una columna, actualiza el enum, no valores sueltos.
- Los literales de `tests/test_schema.py` y `tests/test_repository.py` (rango, número de columnas, celdas
  esperadas) están escritos a mano a propósito: son el contrato con la hoja real. Cuando el esquema cambie,
  fallarán; actualízalos A MANO, no los sustituyas por constantes importadas.
- La escritura usa `RAW` + `INSERT_ROWS`; la lectura `UNFORMATTED_VALUE` + `SERIAL_NUMBER`.
- Las operaciones de `GastosService` se serializan con un `threading.Lock`; no lo quites.
- El MCP solo añade filas: no hay herramientas para editar ni borrar, y no se añaden.
- Las columnas F (Pack) y G (Descripción) las rellena Andrés a mano; el MCP las deja vacías.
- Cambiar el esquema de entrada de `append_receipt_items` cambia el contrato con Claude: menciónalo en
  la descripción del PR.

### Tarea en curso
- Lee `claude/contexto-chat7.md` (ignorado por git) antes de tocar nada de la columna I.

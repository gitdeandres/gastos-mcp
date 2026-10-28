# Despliegue en Cloud Run

Guía del despliegue manual del MCP de Gastos. Recoge los comandos usados el 15/10/2026 (copia de pruebas y producción) y
los procedimientos de despliegue de versiones nuevas, vuelta atrás, rotación del token y diagnóstico.

Todo lo de esta guía se ha ejecutado salvo lo marcado como **propuesta** o **supuesto**.

## Estado tras el primer despliegue (15/10/2026, a modo de ejemplo)

- Servicio `gastos-mcp`, `us-central1`, revisión `gastos-mcp-00002-mig` al 100 %, fijada **por nombre** (no sigue a "la última").
  Apunta a la hoja de **producción**. Imagen `sha-7544edd7a437` (= `v0.2.0`).
- Configuración: timeout 60 s, máximo 1 instancia a nivel de servicio, mínimo 0, `startup-cpu-boost`, cuenta `gastos-sheets`.
- La revisión `gastos-mcp-00001-2xh` (copia de pruebas) está **borrada**. No hay ningún entorno de pruebas desplegado.
- Etiqueta git `v0.2.0` sobre el commit `7544edd7a437`.

## Variables (hay que volver a exportarlas en cada terminal nueva)

```bash
# Sustituye TU-PROYECTO-GCP por el ID de tu proyecto de Google Cloud (el resto son nombres sugeridos).
export PROJECT_ID=TU-PROYECTO-GCP REGION=us-central1 REPO=gastos-mcp SECRET=gastos-mcp-token SERVICE=gastos-mcp
export SA_EMAIL=gastos-sheets@${PROJECT_ID}.iam.gserviceaccount.com
export IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/gastos-mcp
export SHORT=<12 caracteres del commit de la imagen>      # fijarlo a mano: main puede haber avanzado
gcloud config set project $PROJECT_ID
```

El aviso del "quota project" de las credenciales ADC al ejecutar `gcloud config set project` es inofensivo en local.

## 1. Infraestructura (una sola vez, ya hecho)

```bash
# Facturación (la cuenta ya existe y es de pago)
gcloud billing projects describe $PROJECT_ID          # billingEnabled: true

# APIs. NO habilitar containerscanning ni ondemandscanning (se cobran)
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

# Repositorio de imágenes y política de limpieza (3 versiones más recientes)
gcloud artifacts repositories create $REPO --repository-format=docker \
  --location=$REGION --description="Imágenes del MCP de Gastos"
gcloud artifacts repositories set-cleanup-policies $REPO --location=$REGION \
  --policy=deploy/cleanup-policy.json --no-dry-run

# Token: 43 caracteres base64url, sin salto de línea y sin pasar por la pantalla
python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))' \
  | gcloud secrets create $SECRET --replication-policy=automatic --data-file=-
gcloud secrets versions access 1 --secret=$SECRET | wc -c     # 43

# Acceso al secreto solo para la cuenta de servicio, solo sobre ese secreto
gcloud secrets add-iam-policy-binding $SECRET \
  --member="serviceAccount:$SA_EMAIL" --role=roles/secretmanager.secretAccessor

# Docker usa las credenciales de gcloud para este registro
gcloud auth configure-docker ${REGION}-docker.pkg.dev
```

Presupuesto de alerta: consola de Facturación → Presupuestos y alertas (1 USD, toda la cuenta).

## 2. Construir y subir una imagen

Solo si el código de la imagen cambió. Comprobarlo primero (el contexto de build es una lista de permitidos, `.dockerignore`):

```bash
git diff --stat <commit de la imagen actual> HEAD -- pyproject.toml uv.lock .python-version src Dockerfile
```

Si sale vacío, **no hace falta reconstruir**: se despliega la imagen que ya está en el registro (así se hizo el 15/10/2026,
aunque `main` tenía un commit posterior de documentación). Si no, siempre desde un commit de `main` ya fusionado, en un
worktree limpio:

```bash
cd <ruta de tu clon del repositorio>
git fetch origin && git switch main && git pull --ff-only
git rev-parse HEAD origin/main                # deben coincidir
grep '^version' pyproject.toml                # versión que se va a etiquetar (subirla a mano antes)

export VERSION=v0.2.1                          # la que corresponda
export COMMIT=$(git rev-parse HEAD) SHORT=$(git rev-parse --short=12 HEAD)
git worktree add --detach /tmp/gastos-build "$COMMIT"
docker build --network=host \
  --label org.opencontainers.image.revision="$COMMIT" \
  --label org.opencontainers.image.version=$VERSION \
  -t "$IMAGE:$VERSION" -t "$IMAGE:sha-$SHORT" /tmp/gastos-build
git worktree remove /tmp/gastos-build

docker push --all-tags "$IMAGE"
gcloud artifacts docker images list "$IMAGE" --include-tags \
  --format="table(version.basename(),tags,metadata.imageSizeBytes)"
```

`--network=host` solo hace falta si el DNS de tu Docker no resuelve dentro de los contenedores (en el equipo del autor era así).

## 3. Primer despliegue, contra la copia de pruebas (hecho; la revisión `gastos-mcp-00001-2xh` ya no existe)

Se conserva como referencia de cómo se creó el servicio (cuenta, secreto, límites). No repetirlo contra el servicio actual:
`gcloud run deploy` sobre un servicio existente hereda su configuración.

```bash
export TEST_SHEET=$(grep '^TEST_SPREADSHEET_ID=' .env | cut -d= -f2-)
gcloud run deploy $SERVICE \
  --image="$IMAGE:sha-$SHORT" --region=$REGION \
  --service-account="$SA_EMAIL" \
  --set-secrets=MCP_AUTH_TOKEN=$SECRET:1 \
  --set-env-vars=SPREADSHEET_ID="$TEST_SHEET" \
  --max=1 --cpu-boost --allow-unauthenticated \
  --labels=commit=$SHORT,env=test
```

Comprobaciones (sirven también tras cualquier despliegue; con la URL de una etiqueta, sustituir `$URL`):

```bash
gcloud run services describe $SERVICE --region=$REGION
gcloud run revisions describe <REVISION> --region=$REGION --format='value(status.imageDigest)'
gcloud run services describe $SERVICE --region=$REGION --format=export \
  | grep -E "cpu-throttling|startup-cpu-boost|maxScale|minScale|timeoutSeconds"

URL=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)')
TOKEN=$(gcloud secrets versions access 1 --secret=$SECRET)
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
H=(-H "content-type: application/json" -H "accept: application/json, text/event-stream")
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL/mcp" "${H[@]}" -d "$INIT"                       # 401
curl -s -X POST "$URL/mcp" "${H[@]}" -H "authorization: Bearer $TOKEN" -d "$INIT" -w "\n%{http_code}\n"  # 200
npx @modelcontextprotocol/inspector@latest --cli "$URL/mcp" --transport http \
  --header "authorization: Bearer $TOKEN" --method tools/call --tool-name list_categories
unset TOKEN
```

## 4. Desplegar una versión nueva / paso a producción (ejecutado el 15/10/2026)

Principio: revisión nueva **sin tráfico y con etiqueta**, prueba de **solo lecturas** por la URL de la etiqueta, migración a
**esa** revisión y comprobación. Tras la migración el servicio queda fijado por nombre a una revisión: un
`gcloud run deploy` o `gcloud run services update` sin migración explícita **no sale a producción**. Es deseado.

Durante la prueba puede haber dos instancias (las revisiones etiquetadas arrancan, pero solo cuentan para el máximo de
instancias si forman parte de un reparto de tráfico, según la documentación de Cloud Run). Por eso la prueba es solo de
lecturas y no se registran tickets desde Claude mientras dura.

**0. Comprobaciones previas** (todas de solo lectura)

```bash
gcloud artifacts docker images list "$IMAGE" --include-tags --format="table(version.basename(),tags,metadata.imageSizeBytes)"
gcloud run services describe $SERVICE --region=$REGION --format='yaml(status.traffic,status.latestReadyRevisionName)'
```

**1. Revisión nueva sin tráfico y con etiqueta.** El primer paso a producción necesitó el ID de la hoja: solo existe en
Cloud Run, se escribe sin eco y nunca va al `.env`:

```bash
read -rs -p "ID hoja de PRODUCCIÓN: " PROD_SHEET; echo
gcloud run deploy $SERVICE --image="$IMAGE:sha-$SHORT" --region=$REGION \
  --update-env-vars=SPREADSHEET_ID="$PROD_SHEET" \
  --update-labels=env=prod --timeout=60 \
  --no-traffic --tag=prod-check
unset PROD_SHEET
```

En despliegues posteriores no hace falta repetir `--update-env-vars` ni `--timeout`: **supuesto** (gcloud hereda la
configuración de la revisión actual). Comprobarlo antes de migrar, sin imprimir el ID:
`gcloud run services describe $SERVICE --region=$REGION --format=export | grep -c SPREADSHEET_ID` (debe dar 1) y el
`timeoutSeconds` de la plantilla. Al pegar salidas de `describe` en cualquier sitio, ocultar el ID de la hoja.

**2. Comprobar el tráfico.** Debe mostrar la revisión anterior al 100 % y la nueva con `tag: prod-check` al 0 %. Si aparece
`latestRevision: true` al 100 %, parar: producción recibiría tráfico sin haber sido probada.

```bash
gcloud run services describe $SERVICE --region=$REGION --format='yaml(status.traffic)'
```

**3. Probar solo lecturas** por la URL de la etiqueta (`https://prod-check---gastos-mcp-<hash>.<región>.run.app`, la imprime
el deploy). Reutilizar `H` e `INIT` de la sección 3:

```bash
export CHECK_URL=<URL de la etiqueta, sin /mcp>
TOKEN=$(gcloud secrets versions access 1 --secret=$SECRET)
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$CHECK_URL/mcp" "${H[@]}" -d "$INIT"                     # 401
npx @modelcontextprotocol/inspector@latest --cli "$CHECK_URL/mcp" --transport http \
  --header "authorization: Bearer $TOKEN" --method tools/call --tool-name list_categories > /tmp/cats.json
grep -c '__PRUEBA__' /tmp/cats.json                                                                           # 0 en producción
unset TOKEN
```

Cómo distinguir producción de la copia: la copia contenía filas de prueba (`__PRUEBA__`) y las categorías de un ticket de
prueba; producción no las tenía. Es un indicio indirecto: la confirmación fuerte es la primera escritura en la
hoja real.

**4. Migrar a la revisión probada** (con `--to-tags`, no `--to-latest`: este último apunta a la última revisión *creada*,
que podría no ser la probada), quitar la etiqueta y volver a comprobar:

```bash
gcloud run services update-traffic $SERVICE --region=$REGION --to-tags=prod-check=100
gcloud run services update-traffic $SERVICE --region=$REGION --remove-tags=prod-check
gcloud run services describe $SERVICE --region=$REGION --format='yaml(status.traffic)'   # revisión nueva al 100 %, sin latestRevision
gcloud run services describe $SERVICE --region=$REGION --format=export | grep -E "timeoutSeconds|maxScale|startup-cpu-boost|minScale"
gcloud run services describe $SERVICE --region=$REGION | grep -i scaling                 # Auto (Min: 0, Max: 1)
```

**5. En Claude:** una lectura (sin aprobación) y un ticket pequeño. Comparar el `updated_range` devuelto con la última fila
visible de la hoja y revisar fecha y euro, F y G vacías y nada por debajo de la última fila.

**6. Etiqueta git** sobre el commit de la imagen (hecho para v0.2.0):

```bash
git tag -a v0.2.0 7544edd7a437 -m "v0.2.0: primer despliegue en Cloud Run"
git push origin v0.2.0
```

**Primera ejecución (15/10/2026):** revisión `gastos-mcp-00002-mig`; antes del deploy el servicio tenía `latestRevision: true`
y tras `--no-traffic` quedó fijado a `00001-2xh` (100 %); la prueba de lectura no mostró `__PRUEBA__`; primera escritura real
en `A39:H43`. Después se borró `00001-2xh` (apuntaba a la copia).

### Volver atrás

No queda ninguna revisión que apunte a la copia de pruebas (borrada). Opciones:

- Corte inmediato sin escribir en ningún sitio: en Claude, desactivar el conector o poner la escritura en **Bloqueado**.
- Problema de código: desplegar una imagen anterior por su etiqueta `sha-…` con este mismo procedimiento (sin tráfico,
  etiqueta, prueba de lecturas, `--to-tags`). La revisión anterior sigue existiendo mientras no se borre:
  `--to-revisions=<REVISION>=100` devuelve el tráfico a una revisión de producción. Antes de hacerlo, comprobar con
  `describe` que esa revisión apunta a producción (que `SPREADSHEET_ID` es el de producción y no el de la copia, sin
  imprimir el valor en pantallas compartidas).

## 5. Conector en Claude

1. Copiar el valor de la cabecera sin mostrarlo (X11 o XWayland):

   ```bash
   printf 'Bearer %s' "$(gcloud secrets versions access <VERSIÓN> --secret=$SECRET)" | xclip -selection clipboard
   ```

2. Web: **Customize → Connectors → Add custom connector**. Nombre `Gastos`, URL `<URL del servicio>/mcp`.
3. Autenticación: **Sin inicio de sesión**. El diálogo marcará "Iniciar sesión ahora — Detectado": hay que cambiarlo.
4. Encabezados de solicitud: `authorization`, pegar el valor (empieza por `Bearer `), **Requerido**. Transporte: HTTP
   transmisible.
5. Vaciar el portapapeles: `printf '' | xclip -selection clipboard`.
6. Permisos: herramientas de solo lectura en **Permitir siempre**; "Escribir los ítems de un ticket en la hoja" en
   **Requiere aprobación**. En el diálogo de aprobación, pulsar siempre **Permitir una vez**, nunca "Permitir siempre".

## 6. Rotación del token

No hay ventana sin corte: el servidor acepta un solo token, y la autenticación del conector no se puede editar.
Durante unos minutos Claude recibirá 401.

**Atención:** como el servicio queda fijado por nombre a una revisión (sección 4), una revisión nueva creada con
`services update` **no recibe tráfico** y seguiría sirviendo la versión anterior del secreto. Hay que migrar el tráfico
explícitamente (paso 3). **Supuesto** basado en la documentación de Cloud Run sobre el reparto de tráfico; no probado con
una rotación real: comprobar el tráfico con `describe` antes de cambiar el conector.

```bash
# 1. Versión nueva del secreto
python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))' \
  | gcloud secrets versions add $SECRET --data-file=-
gcloud secrets versions list $SECRET                        # anotar el número nuevo (N)
gcloud secrets versions access N --secret=$SECRET | wc -c   # 43

# 2. Revisión nueva con la versión N
gcloud run services update $SERVICE --region=$REGION --update-secrets=MCP_AUTH_TOKEN=$SECRET:N

# 3. Esa revisión no tiene tráfico: migrarlo a ella
NEW=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.latestCreatedRevisionName)')
echo $NEW                                                   # debe ser la que se acaba de crear
gcloud run services update-traffic $SERVICE --region=$REGION --to-revisions=$NEW=100
gcloud run services describe $SERVICE --region=$REGION --format='yaml(status.traffic)'
```

4. En Claude: quitar el conector y volver a añadirlo con el valor nuevo (sección 5), y repetir los permisos.
5. Comprobar una lectura desde Claude y destruir la versión anterior (las destruidas no se cobran):

   ```bash
   gcloud secrets versions destroy <N-1> --secret=$SECRET
   ```

## 7. Diagnóstico

Peticiones recientes (no despierta la instancia):

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="gastos-mcp"' \
  --freshness=15m --limit=30 \
  --format='table(timestamp,httpRequest.requestMethod,httpRequest.status,httpRequest.userAgent,httpRequest.latency,textPayload)'
```

- Las horas de los logs están en UTC (Madrid: +2 en horario de verano).
- **Instancia apagada:** el último `Finished server process` es posterior a la última petición. Arranque en frío: unos 7 s.
- **Claude conecta bien:** peticiones `Claude-User` con 200. Los errores de herramienta (duplicado) también son 200: el error
  va dentro de la respuesta JSON-RPC.
- **Tras un fallo o un tiempo agotado en `append_receipt_items`:** llamar a `check_receipt` antes de reintentar. El servidor
  rechaza el duplicado, pero conviene saber si la primera escritura llegó a hacerse.
- **La cabecera no llega o es incorrecta** ("Couldn't reach the MCP server"): con el conector ya creado aparecen 401 en
  `POST /mcp` y en `/.well-known/oauth-protected-resource…` / `oauth-authorization-server`. El 401 es idéntico si falta
  la cabecera o si el token es incorrecto (el servidor no distingue ni delata rutas). Antes de pasar a OAuth (alternativa
  prevista si Anthropic retirara las cabeceras de solicitud): volver a crear el conector comprobando el prefijo
  `Bearer `, y si persiste, registrar en el middleware el motivo del rechazo (sin el valor).
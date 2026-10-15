# Despliegue en Cloud Run

Guía del despliegue manual del MCP de Gastos. Recoge los comandos usados y
los procedimientos de producción, vuelta atrás, rotación del token y diagnóstico.

Lo marcado como **propuesta** todavía no se ha ejecutado.

## Variables

```bash
export PROJECT_ID=gastos-mcp-server REGION=us-central1 REPO=gastos-mcp SECRET=gastos-mcp-token SERVICE=gastos-mcp
export SA_EMAIL=gastos-sheets@${PROJECT_ID}.iam.gserviceaccount.com
export IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/gastos-mcp
gcloud config set project $PROJECT_ID
```

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

Siempre desde un commit de `main` ya fusionado, en un worktree limpio.

```bash
cd ~/Dev/gastos-mcp
git fetch origin && git switch main && git pull --ff-only
git rev-parse HEAD origin/main                # deben coincidir
grep '^version' pyproject.toml                # versión que se va a etiquetar

export VERSION=v0.2.0
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

`--network=host` solo hace falta en el equipo de Andrés (DNS de Docker).

## 3. Desplegar contra la copia de pruebas (hecho, revisión `gastos-mcp-00001-2xh`)

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

Comprobaciones:

```bash
gcloud run services describe $SERVICE --region=$REGION
gcloud run revisions describe <REVISION> --region=$REGION --format='value(status.imageDigest)'
gcloud run services describe $SERVICE --region=$REGION --format=export \
  | grep -E "cpu-throttling|startup-cpu-boost|maxScale|minScale"

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

## 4. Paso a producción (propuesta)

El ID de la hoja de producción solo existe en Cloud Run: se escribe sin eco y nunca va al `.env`.

1. Revisión nueva **sin tráfico** y con etiqueta, para probarla por su URL propia sin afectar a Claude:

   ```bash
   read -rs -p "ID hoja de PRODUCCIÓN: " PROD_SHEET; echo
   gcloud run deploy $SERVICE --image="$IMAGE:sha-$SHORT" --region=$REGION \
     --update-env-vars=SPREADSHEET_ID="$PROD_SHEET" \
     --update-labels=env=prod --timeout=60 \
     --no-traffic --tag=prod-check
   unset PROD_SHEET
   ```

   La documentación indica que una revisión etiquetada sin tráfico no cuenta para el máximo de instancias del servicio:
   durante la prueba puede haber dos instancias. Por eso, en esta fase, **solo lecturas**.

2. Probar **solo lecturas** contra `https://prod-check---gastos-mcp-<…>.run.app/mcp` (la URL sale en el despliegue) con el
   inspector: `list_categories` debe mostrar las categorías reales, no `__PRUEBA__`.
3. Pasar el tráfico: `gcloud run services update-traffic $SERVICE --region=$REGION --to-latest` y quitar la etiqueta
   (`--remove-tags=prod-check`).
4. Probar desde Claude una lectura y, después, el primer ticket real.
5. Crear y subir la etiqueta git, sobre el mismo commit de la imagen:

   ```bash
   git tag -a v0.2.0 7544edd7a437 -m "v0.2.0: primer despliegue en Cloud Run"
   git push origin v0.2.0
   ```

### Volver atrás

**Cuidado:** la revisión anterior (`gastos-mcp-00001-2xh`) apunta a la **copia de pruebas**. Devolverle el tráfico haría que
los tickets se escribieran en la copia sin ningún aviso. Para volver atrás en producción:

- Problema de código: desplegar la imagen anterior con el ID de producción (`gcloud run deploy … --image=<sha anterior>`),
  no mover el tráfico a una revisión de pruebas.
- Corte inmediato sin escribir en ningún sitio: en Claude, desactivar el conector o poner la escritura en **Bloqueado**.

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
   **Requiere aprobación**. En el diálogo de aprobación, nunca "Permitir siempre".

## 6. Rotación del token

No hay ventana sin corte: el servidor acepta un solo token, y la autenticación del conector no se puede editar.
Durante unos minutos Claude recibirá 401.

```bash
# 1. Versión nueva del secreto
python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))' \
  | gcloud secrets versions add $SECRET --data-file=-
gcloud secrets versions list $SECRET                        # anotar el número nuevo (N)
gcloud secrets versions access N --secret=$SECRET | wc -c   # 43

# 2. Revisión nueva con la versión N (el token anterior deja de valer al terminar las instancias viejas)
gcloud run services update $SERVICE --region=$REGION --update-secrets=MCP_AUTH_TOKEN=$SECRET:N
```

3. En Claude: quitar el conector y volver a añadirlo con el valor nuevo (sección 5), y repetir los permisos.
4. Comprobar una lectura desde Claude y destruir la versión anterior (las destruidas no se cobran):

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
- **La cabecera no llega o es incorrecta** ("Couldn't reach the MCP server"): con el conector ya creado aparecen 401 en
  `POST /mcp` y en `/.well-known/oauth-protected-resource…` / `oauth-authorization-server`. El 401 es idéntico si falta
  la cabecera o si el token es incorrecto (D35). Antes de pasar a la opción B de D12: volver a crear el conector
  comprobando el prefijo `Bearer `, y si persiste, registrar en el middleware el motivo del rechazo (sin el valor).

# qa-review

QA y code review **en un solo paso** para el cambio que tienes entre manos. Funciona como:

- **Servidor MCP** para Claude Code y Kiro: el agente lo usa antes de dar un trabajo por terminado.
- **CLI** para la terminal y para CI: un comando, un informe y un código de salida.

Funciona **solo**. Si encuentra **graphskill** o **agenticOS** en tu máquina, los usa para afinar el
resultado; si no, sigue funcionando igual.

---

## Índice

1. [Cómo funciona](#1-cómo-funciona)
2. [Requisitos](#2-requisitos)
3. [Instalación (una vez por máquina)](#3-instalación-una-vez-por-máquina)
4. [Conectar un proyecto](#4-conectar-un-proyecto)
5. [Uso desde Claude Code](#5-uso-desde-claude-code)
6. [Uso desde Kiro](#6-uso-desde-kiro)
7. [Uso desde la terminal](#7-uso-desde-la-terminal)
8. [Criterios de aceptación (UAC) con evidencias](#8-criterios-de-aceptación-uac-con-evidencias)
9. [Configuración: `.qa-review.yaml`](#9-configuración-qa-reviewyaml)
10. [Qué comprueba](#10-qué-comprueba)
11. [El veredicto](#11-el-veredicto)
12. [Integración con graphskill](#12-integración-con-graphskill)
13. [Integración con agenticOS](#13-integración-con-agenticos)
14. [CI (GitHub Actions)](#14-ci-github-actions)
15. [Referencia de herramientas MCP](#15-referencia-de-herramientas-mcp)
16. [Solución de problemas](#16-solución-de-problemas)
17. [Desarrollo](#17-desarrollo)
18. [Límites conocidos](#18-límites-conocidos)

---

## 1. Cómo funciona

```
 tu cambio (git)
      │
      ├─► reglas de seguridad (fijas, sin IA)
      ├─► impacto: símbolos cambiados → quién los llama → qué tests los cubren
      ├─► checks del proyecto: lint, tipos, tests  (+ e2e con Playwright, API con Schemathesis)
      │
      ▼
  veredicto provisional: pass / warn / block
      │
      ├─► "review packet": diff + impacto + alertas + reglas del equipo
      │        │
      │        ▼
      │   review IA (la hace TU agente: Claude Code, Kiro o un reviewer de agenticOS)
      │        │
      └────────┴─► veredicto final + informe
```

Dos ideas clave:

- **qa-review no llama a ningún modelo.** Ejecuta la parte determinista (checks, tests, reglas, impacto) y
  prepara un paquete de revisión. La revisión la hace el agente que ya usas y la devuelve con
  `qa_submit_review`. Por eso funciona igual en Claude Code, en Kiro y en CI (en CI solo la parte determinista).
- **Revisa "el cambio", no todo el repo.** Por defecto compara contra el *merge-base* con `main`, así que en
  una rama de feature revisa toda la rama (commiteado o no, incluidos archivos nuevos sin trackear). En la propia
  rama `main`/`master` revisa solo lo no commiteado.

### Ejemplo real, paso a paso

**El caso:** en un proyecto Python con API y tests (FastAPI + pytest + Playwright), Alejandro pide a Claude
Code: *"añade un descuento opcional al total del carrito"*. Claude cambia `app/pricing.py` (`total()` ahora
acepta `discount`) y añade un test.

**1. Antes de decir "hecho", el agente lanza QA rápida** (lo hace solo, por la skill):

```
qa_run(level="quick")
```

qa-review calcula el cambio (rama `feature/discount` contra `main`), pasa las reglas de seguridad y pregunta
a graphskill quién llama a `total()`: aparecen `checkout()` en `app/cart.py` e `invoice()` en
`app/billing/invoice.py`. Con eso decide qué tests ejecutar: `tests/test_pricing.py` y `tests/test_cart.py`,
no los 400 del proyecto. Resultado:

```
verdict: block
- check `pytest` fail     → test_cart.py::test_checkout_total  (esperaba 50, recibió 45)
```

Sin el análisis de impacto, ese test no se habría ejecutado: no prueba `pricing.py` directamente, sino a
uno de sus callers.

**2. El agente arregla y repite.** `checkout()` pasaba un argumento por posición que ahora caía en
`discount`. Lo corrige y vuelve a lanzar `qa_run(level="quick")`: los tests pasan.

**3. QA completa contra staging**, antes de abrir el PR:

```
qa_run(level="full", host="staging.miapp.com")
```

Los tests unitarios corren en local. Playwright recibe `BASE_URL=https://staging.miapp.com` y Schemathesis
dispara cientos de peticiones contra el `swagger.yml` de ese host. Todo pasa.

**4. Review independiente.** El agente pide `qa_review_packet()` y se lo pasa a un subagente limpio. El paquete
lleva el diff, los callers de `total()`, las reglas del proyecto y una lección de agenticOS: *"importes siempre
en céntimos enteros"*. El reviewer devuelve:

```
qa_submit_review(verdict="warn", summary="Correcto; el descuento no se limita",
  findings=[{severity: "medium", title: "Un descuento mayor que el total da negativo",
             file: "app/pricing.py", line: 6}])
```

**5. Arreglo final y cierre.** El agente añade `max(0, …)` con su test y repite `qa_run`. Después la review
sale `pass` y comparte el `qa_report`:

```
qa-review ✅ PASS · Target host: https://staging.miapp.com
Checks: pytest ✅ · ruff ✅ · playwright ✅ · schemathesis ✅
Review: pass — descuento acotado y con test
```

**6. En el PR**, GitHub Actions repite la parte determinista (`qa-review run`) y bloquea el merge si algo
vuelve a romperse.

Si alguien pidiera `host="miapp.com"` (producción, en `protected_hosts`), qa-review se negaría salvo que tú
autorizases `allow_protected` de forma explícita.

---

## 2. Requisitos

| Necesario | Para qué |
|---|---|
| Python 3.10 o superior | qa-review |
| git | Calcular el cambio |

| Opcional | Para qué |
|---|---|
| Las herramientas que ya use tu proyecto (ruff, pytest, npm scripts, phpunit, phpstan, go) | Los checks. Solo se ejecutan las que estén instaladas |
| Node + `@playwright/test` | Tests end-to-end y tests de los UAC |
| Chrome, Chromium, Edge o Brave | Informe de UAC en PDF (si no está el Chromium de Playwright) |
| `schemathesis` | Fuzzing de la API contra su OpenAPI/Swagger |
| graphskill | Impacto más preciso (ver [sección 12](#12-integración-con-graphskill)) |
| agenticOS | Lecciones y reglas del equipo en la review (ver [sección 13](#13-integración-con-agenticos)) |

---

## 3. Instalación (una vez por máquina)

> En macOS, crea el entorno virtual **fuera** de `~/Desktop` y `~/Documents`: iCloud puede romper los venvs.

```bash
# 1. Entorno virtual propio
python3 -m venv ~/.qa-review/venv

# 2. Instalar qa-review (editable, con dependencias de desarrollo)
~/.qa-review/venv/bin/pip install -e "$HOME/Projects/qa-review[dev]"

# 3. Dejar el comando a mano
mkdir -p ~/.local/bin
ln -sf ~/.qa-review/venv/bin/qa-review ~/.local/bin/qa-review
#    (asegúrate de que ~/.local/bin está en tu PATH)

# 4. Comprobar
qa-review --version
cd ~/Projects/qa-review && ~/.qa-review/venv/bin/pytest -q     # deben pasar todos los tests
```

---

## 4. Conectar un proyecto

```bash
qa-review init ~/ruta/a/tu-proyecto
```

`init` es idempotente: puedes ejecutarlo varias veces. Si un archivo ya existe lo respeta, y en los
`mcp.json` **añade** el servidor `qa-review` sin tocar los demás (por ejemplo, `graphskill`).

| Archivo que escribe | Para |
|---|---|
| `.qa-review.yaml` | Configuración del proyecto (todo opcional; viene comentada) |
| `.mcp.json` → servidor `qa-review` | Claude Code |
| `.claude/skills/qa-review/SKILL.md` | Le dice a Claude Code cuándo y cómo usarlo |
| `.kiro/settings/mcp.json` → servidor `qa-review` | Kiro |
| `.kiro/steering/qa-review.md` | Regla de Kiro: pasar QA antes de dar algo por hecho |

Opciones:

```bash
qa-review init . --no-kiro                 # solo Claude Code
qa-review init . --no-claude               # solo Kiro
qa-review init . --command "qa-review"     # cómo arrancar el servidor (por defecto: el Python del venv con -m qa_review)
```

Después:

1. Abre (o reinicia) Claude Code o Kiro **en ese proyecto**.
2. Aprueba el servidor MCP `qa-review` cuando te lo pida.
3. Comprueba qué va a ejecutar: `qa-review status` (o pide al agente *"qa_status"*).

> ¿Commitear los archivos? `.qa-review.yaml`, la skill y el steering sí (son del equipo). Los `mcp.json` llevan
> rutas absolutas de tu máquina: compártelos solo si todo el equipo usa la misma ruta, o usa `--command qa-review`.

---

## 5. Uso desde Claude Code

Pídeselo con tus palabras: *"pasa QA y review a este cambio"*, *"¿está listo para merge?"*, *"revisa la rama"*.
La skill instalada guía al agente por este flujo:

1. **`qa_run`** con `level: quick` mientras iteras (solo los tests relacionados con el cambio, sin e2e) y
   `level: full` antes de terminar.
2. Arreglar lo que bloquea y volver a ejecutar `qa_run`.
3. **`qa_review_packet`** y revisar. Si el mismo agente escribió el código, lo ideal es pasar el paquete a un
   **subagente limpio**, para que la review no herede las suposiciones del autor.
4. **`qa_submit_review`** con el veredicto, el resumen, los hallazgos y `cleared_rules`, que son las alertas de
   seguridad comprobadas que resultaron ser falsos positivos.
5. **`qa_report`** con el informe final. Nunca dar por hecho un cambio con veredicto `block`.

---

## 6. Uso desde Kiro

Igual que en Claude Code: el servidor está en `.kiro/settings/mcp.json` y el *steering*
(`.kiro/steering/qa-review.md`, con `inclusion: always`) le recuerda a Kiro que pase QA antes de terminar.

Para automatizarlo más puedes crear un **agent hook** en Kiro (por ejemplo, manual o al terminar una tarea de
una spec) cuyo prompt sea: *"Ejecuta qa_run con level quick, arregla lo que bloquee y resume el qa_report"*.

---

## 7. Uso desde la terminal

```bash
qa-review status                      # checks detectados e integraciones activas
qa-review run                         # QA del cambio actual; informe en markdown
qa-review run --level quick           # solo tests relacionados, sin e2e/API
qa-review run --base develop          # comparar contra otra rama o commit
qa-review run --no-checks             # solo reglas de seguridad + impacto (segundos)
qa-review run --format json           # salida para máquinas
qa-review run --fail-on warn          # más estricto: también falla con warn
qa-review run --host staging.miapp.com --only e2e,api   # contra otro entorno (ver abajo)
qa-review packet                      # paquete de review del último run (para pegarlo a un agente)
qa-review report                      # informe del último run
qa-review report --run <id> --format json
qa-review serve                       # servidor MCP por stdio (lo arranca tu IDE, no tú)
```

Todos aceptan una ruta al proyecto como primer argumento (por defecto, el directorio actual).

### Apuntar las pruebas a un host

Con `--host` (o `host` en `qa_run`) eliges contra qué entorno corren las pruebas, sin tocar la configuración:

```bash
qa-review run --host localhost:8000                    # tu máquina   → http://localhost:8000
qa-review run --host app:8080                          # servicio de docker compose → http://app:8080
qa-review run --host staging.miapp.com --only e2e,api  # staging, solo e2e y API → https://staging.miapp.com
```

Cada check recibe el host de dos formas:

- **Variables de entorno:** `BASE_URL` (configurable con `host_env`) y siempre `QA_REVIEW_HOST`. Es lo que
  leen Playwright (`use: { baseURL: process.env.BASE_URL }` en `playwright.config`), los tests de API
  y tus scripts.
- **`{host}` dentro del comando**, ya escapado para la shell: `cmd: "curl -fsS {host}/health"`. Si un check usa
  `{host}` y no se pasa ninguno, sale `skipped` en vez de fallar.

Schemathesis usa el host como `--url` (y el host de la ejecución gana sobre el `base_url` configurado).

Reglas:

- Sin esquema, se completa solo: `http://` para `localhost`, IPs internas, nombres sin punto (servicios de
  docker), `.local` y `.test`; `https://` para el resto.
- Solo se aceptan URLs `http(s)` sin caracteres de shell; cualquier otra cosa da error (código 2).
- **Hosts protegidos (producción):** lista `protected_hosts` en `.qa-review.yaml`. Un host protegido (o un
  subdominio suyo) solo se ejecuta con `--allow-protected`. El agente tiene instrucciones de no usarlo nunca
  si no se lo pides tú expresamente.

**Códigos de salida de `run`:** `0` pass o warn · `1` block (o warn con `--fail-on warn`) · `2` error
(no es un repo git, configuración inválida…).

Cada run se guarda en `<proyecto>/.qa-review/runs/<id>.json`. Esa carpeta lleva su propio `.gitignore`,
así que nunca acaba en un commit.

---

## 8. Criterios de aceptación (UAC) con evidencias

qa-review puede convertir una tarea en **criterios de aceptación (UAC) en Gherkin**, generar a partir de
ellos una **batería de tests Playwright (E2E y API)**, ejecutarla contra el host que elijas y entregar un
**informe en PDF y/o HTML con las evidencias de cada UAC**: capturas de pantalla, peticiones y respuestas,
errores, trazas y vídeo de los fallos.

Igual que en la review, qa-review no llama a ningún modelo: tu agente redacta los UAC y los tests a partir
de los paquetes que prepara qa-review, y qa-review los valida, guarda, ejecuta y documenta.

### De dónde salen los UAC

| Origen | Cómo se usa | Qué lee qa-review |
|---|---|---|
| **Spec de Kiro** | `source="kiro"`, `ref="<carpeta de la spec>"` | `.kiro/specs/<spec>/requirements.md` (con sus criterios EARS numerados `1.1`, `1.2`…), `design.md` y `tasks.md` |
| **Board de agenticOS** | `source="agenticos"`, `ref="<id de la tarea>"` | Título y spec de la tarea, brief y contrato de la feature (solo lectura de `~/.agenticos/data/board.db`) |
| **Jira / Linear** | El agente lee la issue con su conector y la pasa como `task_text` (`source="jira"`, `ref="PROJ-123"`) | El texto que le pasa el agente |
| **El código** | Siempre, automáticamente | El diff de la rama: endpoints añadidos (FastAPI, Flask, Express, Laravel, Symfony, Nest/Spring, Django), páginas tocadas y símbolos cambiados |

La tarea manda sobre **qué** debe pasar. El código **completa** los criterios con lo que la tarea no dice
(validaciones, errores, endpoints). Si el código no hace algo que la tarea pide, el UAC se escribe igualmente:
su test fallará, y eso es justo lo que el informe debe mostrar.

### El flujo

```
uac_sources ─► uac_draft_packet ─► (el agente escribe Gherkin) ─► uac_save
            ─► uac_tests_packet ─► (el agente escribe tests Playwright) ─► uac_run ─► informe PDF / HTML
```

1. **Borrador.** `uac_draft_packet(source="kiro", ref="cart-discount")` devuelve la tarea, los criterios que ya
   trae, lo que cambió en el código y las reglas para escribir los UAC.
2. **Guardar.** El agente escribe un `.feature` (un `Scenario` por UAC) y lo guarda con `uac_save`, que lo valida:
   - cada escenario lleva un `@UAC-<n>` único y `@e2e` (UI) y/o `@api` (HTTP);
   - `@source:task` o `@source:code` indica de dónde sale, y `@ref:1.2` / `@ref:PROJ-123` lo enlaza con el original;
   - la `Feature` lleva `@task:<ref>` para trazar el informe hasta la tarea.

   Se guarda en `qa/uac/<slug>.feature`, versionado con el código.
3. **Tests.** `uac_tests_packet` crea (si no existen) `playwright.uac.config.ts` y `tests/uac/evidence.ts`, y explica
   cómo escribir `tests/uac/<slug>.spec.ts`: el título de cada test empieza por `[UAC-n]`, y los helpers de evidencia
   registran todo:
   - `shot(page, 'etiqueta')`: captura de pantalla tras cada *Then* visible;
   - `apiCall(request, 'POST', '/api/…', datos)`: guarda petición y respuesta (con cabeceras sensibles ocultas);
   - `note('texto')`: cualquier dato relevante (ids creados, valores leídos).
4. **Ejecutar.** `uac_run(slug, host="staging.miapp.com", format="pdf,html")` lanza los tests contra ese host
   (`BASE_URL`), asocia cada resultado a su UAC y escribe el informe.

### Estado de cada UAC y veredicto

| Estado | Cuándo |
|---|---|
| Superado | Todos sus tests pasan |
| Fallido | Algún test falla |
| Inestable | Algún test pasó solo tras reintentar |
| Omitido | Todos sus tests se saltaron |
| Sin cubrir | Ningún test lleva su `[UAC-n]` |

Veredicto global: **ACEPTADO** si todos están superados; **NO ACEPTADO** si alguno falla; **INCOMPLETO** si hay
alguno sin cubrir, omitido o inestable. Los tests que no mencionan ningún UAC aparecen aparte, como "tests sin
criterio asociado".

### El informe

Un único HTML autocontenido (imágenes incrustadas) del que se imprime el PDF. Contiene:

- **Cabecera:** proyecto, tarea, host, rama y commit (avisa si hay cambios sin commitear), fecha, ejecución y duración.
- **Veredicto** con el recuento por estado y una **tabla resumen** de los UAC con su tipo, origen, referencia y tests.
- **Una sección por UAC** (en el PDF, una página nueva cada una): el escenario Gherkin y, por cada test, su resultado,
  el error si falló y sus evidencias (capturas, petición/respuesta, notas y rutas a traza y vídeo).
- **Detalles de la ejecución:** el comando y la salida.

**Formato configurable:** `uac.report` en `.qa-review.yaml` (`[pdf, html]` por defecto), o `--format` / `format` en
cada ejecución (`pdf`, `html`, `pdf,html` o `both`). `qa-review uac report <slug> --format html` regenera el informe
de la última ejecución sin volver a lanzar los tests.

**Cómo se genera el PDF** (por orden): con el Chromium de Playwright del proyecto y, si no está, con un Chrome,
Chromium, Edge o Brave instalado (`QA_REVIEW_CHROME` fuerza una ruta concreta). Si no hay ninguno, se entrega el
HTML y se indica el motivo; nunca te quedas sin informe.

Los informes, capturas y resultados se guardan en `.qa-review/uac/<slug>/<ejecución>/` (ignorado por git).

### Desde la terminal

```bash
qa-review uac sources                                    # specs de Kiro, board de agenticOS, UAC guardados
qa-review uac draft --source kiro --ref cart-discount    # paquete para redactar los UAC
qa-review uac draft --source jira --ref PROJ-123 --text-file issue.md
qa-review uac save cart-discount cart-discount.feature   # valida y guarda (o "-" para stdin)
qa-review uac show cart-discount
qa-review uac tests cart-discount                        # andamiaje + paquete para escribir los tests
qa-review uac run cart-discount --host staging.miapp.com --format pdf,html
qa-review uac report cart-discount --format html         # regenerar el informe del último run
```

`uac run` devuelve `0` si el veredicto es ACEPTADO, `1` si no y `2` si hay un error (sin host, sin tests, host
protegido…).

### Requisitos de esta parte

- Node y `@playwright/test` en el proyecto: `npm i -D @playwright/test && npx playwright install chromium`
  (o usa tu Chrome con `QA_REVIEW_BROWSER_CHANNEL=chrome`).
- Para grabar vídeo de los fallos, Playwright necesita `ffmpeg` (`npx playwright install ffmpeg`), o desactívalo
  con `QA_REVIEW_VIDEO=off`.
- Si la API exige token: `QA_REVIEW_API_TOKEN` se envía como `Authorization: Bearer …` en todas las peticiones.

## 9. Configuración: `.qa-review.yaml`

Todo es opcional. Sin este archivo, los checks se **detectan solos** (ver [sección 10](#10-qué-comprueba)).

```yaml
# Contra qué comparar (por defecto: merge-base con main/master)
base: main

# Lista EXACTA de checks. Si la defines, sustituye a la detección automática.
checks:
  - name: lint
    cmd: "ruff check ."
    kind: lint                          # lint | types | tests | security | e2e | api | check
    blocking: false                     # si falla: warn en vez de block
    targeted_cmd: "ruff check {targets}"  # versión limitada a los archivos cambiados (nivel quick)
  - name: tests
    cmd: "pytest -q"
    kind: tests
    targeted_cmd: "pytest -q {targets}"   # en quick, {targets} = tests relacionados con el cambio

# QA funcional
qa:
  playwright: {cmd: "npx playwright test --reporter=line"}   # o "off"; se detecta si hay playwright.config.*
  openapi:
    spec: openapi.yaml
    base_url: "http://localhost:8000"   # la API tiene que estar levantada
    # cmd: "schemathesis run openapi.yaml --url http://localhost:8000"   # alternativa: comando propio

# Reglas propias que el reviewer debe comprobar
review:
  rules:
    - "Todo endpoint valida su entrada"
    - "Los importes van en céntimos enteros"

# Integraciones opcionales
integrations:
  graphskill: auto        # auto | off | "<comando>", p. ej. "/ruta/venv/bin/python -m graphskill"
  agenticos: auto         # auto | off

# Tiempo máximo por check, en segundos
timeout_sec: 600

# Host por defecto de las pruebas (se puede cambiar en cada ejecución con --host)
host: http://localhost:8000
host_env: [BASE_URL, API_URL]     # variables que reciben el host (QA_REVIEW_HOST siempre)
protected_hosts: [miapp.com]      # producción: solo con --allow-protected

# Criterios de aceptación (UAC)
uac:
  dir: qa/uac                     # archivos .feature (uno por tarea), versionados
  tests_dir: tests/uac            # tests Playwright generados
  playwright_config: playwright.uac.config.ts
  report: [pdf, html]             # pdf, html o los dos
  output_dir: .qa-review/uac      # ejecuciones, evidencias e informes
  language: es                    # idioma de los UAC y del informe (es | en)
```

**Niveles:**

| Nivel | Qué ejecuta |
|---|---|
| `quick` | Seguridad + impacto + checks; los que tienen `targeted_cmd` se limitan al cambio. Sin e2e ni API |
| `full` | Todo, sin limitar. Incluye Playwright y Schemathesis si están configurados |

---

## 10. Qué comprueba

### Checks detectados automáticamente

Solo se proponen las herramientas que **están instaladas**. Primero busca en el entorno del proyecto
(`.venv/bin`, `venv/bin`, `node_modules/.bin`, `vendor/bin`) y después en el `PATH`.

| Stack (se detecta por) | Checks |
|---|---|
| Python (`pyproject.toml`, `setup.py`, `requirements.txt`) | `ruff` (no bloqueante), `pytest` si hay carpeta de tests |
| Node (`package.json`) | scripts `lint` (no bloqueante), `typecheck`/`type-check`, `test` con npm, pnpm o yarn según el lockfile |
| PHP (`composer.json`) | `phpstan` (no bloqueante), `phpunit` |
| Go (`go.mod`) | `go vet`, `go test` |
| Playwright (`playwright.config.*`) | `playwright test` |
| OpenAPI/Swagger | Schemathesis si configuras `qa.openapi`; si encuentra un spec sin configurar, te lo sugiere |

### Reglas de seguridad (sobre las líneas añadidas)

| Regla | Severidad | Detecta |
|---|---|---|
| `secret-literal` / `secret-token` | critical | Contraseñas, API keys, tokens o claves privadas escritos en el código (el valor nunca se muestra) |
| `sql-interp` | high | SQL construido con interpolación o concatenación |
| `shell` | high | `os.system`, `shell=True`, `child_process.exec`, `shell_exec`… |
| `eval` | high | `eval`, `exec`, `new Function` |
| `deserialize` | high | `pickle.loads`, `yaml.load` sin SafeLoader, `unserialize` |
| `path-from-input` | high | Rutas de archivo construidas con datos de la petición |
| `tls-off` | high | Verificación TLS desactivada |
| `env-file` | high | Un `.env` dentro del cambio |
| `weak-hash` | medium | MD5 o SHA-1 |
| `debug-on` | medium | Modo debug activado |
| `cors-any` | medium | CORS abierto a cualquier origen |
| `xss-sink` | medium | `innerHTML`, `dangerouslySetInnerHTML`, `|safe`, `{!!` |

En los archivos de test solo se aplican las reglas de secretos. Además, si el cambio toca rutas sensibles
(auth, login, session, token, payment, `.env`…), el paquete de review añade un **checklist de seguridad**.

### Impacto

- **Símbolos cambiados:** funciones y clases cuyas líneas toca el cambio.
- **Callers:** quién llama a esos símbolos (con graphskill, a través del grafo; sin él, con `git grep`).
- **Tests relacionados:** los que mencionan el módulo en su nombre (`test_cart.py`, `cart.test.ts`,
  `CartTest.php`, `test_checkout_cart.py`…) más los que son callers directos.
- **Sin test:** archivos de código cambiados para los que no se encontró ningún test.

---

## 11. El veredicto

Se decide con reglas fijas, para que sea predecible:

| Veredicto | Cuándo |
|---|---|
| ⛔ **block** | Falla un check bloqueante (o se agota su tiempo, o no se puede ejecutar), hay un hallazgo `critical`, el reviewer reporta algo `high` o el reviewer bloquea |
| ⚠️ **warn** | Falla un check no bloqueante, hay un hallazgo `medium` o superior, queda una alerta de seguridad sin descartar o hay código cambiado sin test |
| ✅ **pass** | Nada de lo anterior |

Una alerta automática `high` (por ejemplo, `shell`) da **warn**, no block, porque es una heurística. El
reviewer la confirma (como hallazgo) o la descarta con `cleared_rules`. Los secretos (`critical`) bloquean
siempre.

---

## 12. Integración con graphskill

Con graphskill, el impacto es exacto: rangos de símbolos reales en cualquier lenguaje que indexe y el grafo
de llamadas hasta profundidad 2. qa-review abre una sesión MCP corta con el servidor de graphskill del
proyecto; no necesita importar graphskill.

Lo busca en este orden (gana el primero):

1. `integrations.graphskill` en `.qa-review.yaml` con un comando: `"/ruta/venv/bin/python -m graphskill"`.
2. La entrada `graphskill` del `.mcp.json` del proyecto (la que escribe `graphskill setup`).
3. La variable de entorno `QA_REVIEW_GRAPHSKILL` con un comando.
4. Un ejecutable `graphskill` en el `PATH`.

Si no lo encuentra o no responde, usa heurísticas (`ast` en Python, regex de definiciones en otros
lenguajes y `git grep` para los callers) y lo indica en el informe. **Nunca falla por su ausencia.**

Pasos recomendados:

```bash
graphskill setup ~/ruta/a/tu-proyecto     # indexa y escribe la entrada en .mcp.json
qa-review init  ~/ruta/a/tu-proyecto     # añade qa-review junto a graphskill
qa-review status ~/ruta/a/tu-proyecto    # debe decir "graphskill: on, from the project's .mcp.json"
```

---

## 13. Integración con agenticOS

### agenticOS → qa-review (lectura)

Si el proyecto está enlazado con `aos link`, el paquete de review incluye:

- las **lecciones** del proyecto: las confirmadas y las no revisadas con confianza ≥ 0.5 (las rechazadas
  nunca entran);
- las **reglas del equipo** de `AGENTS.md`.

Así el reviewer comprueba el cambio contra lo que el equipo ya ha aprendido. qa-review solo **lee** los
archivos de `~/.agenticos` (o de `QA_REVIEW_AGENTICOS_HOME`); no escribe lecciones, porque eso lo valida el
learner de agenticOS.

### qa-review → agenticOS (board)

Para que los workers del board pasen qa-review antes de dar una tarea por hecha, define el `verify_command`
del proyecto en `~/.agenticos/data/projects.yaml`:

```yaml
worker:
  verify_command: "~/.qa-review/venv/bin/qa-review run . --ci"
```

Se ejecuta en el worktree de la tarea. Si sale `block` (código 1), agenticOS rechaza el intento y el
informe sirve de motivo para el siguiente. `qa-review init` te muestra la línea exacta para tu instalación.

Comprobación: `qa-review status` debe mostrar `agenticos: on, project '<slug>'`.

---

## 14. CI (GitHub Actions)

```yaml
jobs:
  qa-review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0                     # necesario para calcular el merge-base
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "git+https://github.com/<tu-org>/qa-review.git"   # o la ruta donde lo tengas
      - run: pip install -r requirements.txt   # las herramientas de tu proyecto (pytest, ruff…)
      - name: qa-review
        run: |
          status=0
          qa-review run --base "origin/${{ github.base_ref }}" > qa-report.md || status=$?
          cat qa-report.md >> "$GITHUB_STEP_SUMMARY"
          exit $status
```

En CI solo corre la parte determinista. El informe aparece en el resumen del job y el paso falla con `block`.

---

## 15. Referencia de herramientas MCP

Todas aceptan `project` (ruta) para trabajar sobre otro proyecto distinto del que arrancó el servidor.

| Herramienta | Parámetros | Devuelve |
|---|---|---|
| `qa_status` | — | Checks que se ejecutarían, sugerencias, estado de las integraciones |
| `qa_run` | `base`, `level` (`quick`/`full`), `paths`, `only` (nombres o tipos de check), `host`, `allow_protected` | Resumen: `run_id`, veredicto, motivos, checks (con la salida de los que fallan), alertas, impacto y siguiente paso |
| `qa_review_packet` | `run_id` (por defecto el último), `max_diff_chars` | Texto con checklist, reglas, lecciones, alertas, checks fallidos, impacto y diff |
| `qa_submit_review` | `run_id`, `verdict` (`pass`/`warn`/`block`), `summary`, `findings`, `cleared_rules`, `reviewer` | Veredicto actualizado |
| `qa_report` | `run_id`, `format` (`markdown`/`json`) | Informe completo |
| `qa_impact` | `base` | Solo el impacto, sin ejecutar checks |
| `qa_security_scan` | `base` | Solo las reglas de seguridad, sin ejecutar checks |
| `uac_sources` | — | Specs de Kiro, estado de agenticOS y UAC ya guardados |
| `uac_draft_packet` | `source` (`kiro`/`agenticos`/`jira`/`linear`/`text`), `ref`, `task_text`, `title`, `slug`, `base` | Tarea, criterios existentes, cambios del código y reglas para escribir los UAC |
| `uac_save` | `slug`, `gherkin` | UAC validados y guardados (o la lista de problemas) |
| `uac_show` | `slug` | Los UAC guardados, ya interpretados |
| `uac_tests_packet` | `slug` | Crea el andamiaje y explica cómo escribir los tests |
| `uac_run` | `slug`, `host`, `format`, `allow_protected` | Veredicto, estado por UAC y rutas del informe |
| `uac_report` | `slug`, `run_id`, `format` | Regenera el informe de una ejecución en otro formato |

Formato de cada hallazgo en `findings`:

```json
{"severity": "high", "title": "El descuento puede dejar el total en negativo",
 "file": "app/pricing.py", "line": 6, "detail": "falta max(0, ...)"}
```

Severidades: `info`, `low`, `medium`, `high`, `critical`.

---

## 16. Solución de problemas

| Síntoma | Qué hacer |
|---|---|
| `qa-review: ... is not a git repository` | Ejecútalo dentro de un repo git, o pasa la ruta del proyecto |
| "no checks detected" | Las herramientas no están instaladas en el entorno del proyecto. Instálalas o define `checks:` en `.qa-review.yaml` |
| Un check sale `error` | El comando no existe (código 127). Revisa `qa-review status` y la ruta de la herramienta |
| Un check sale `timeout` | Sube `timeout_sec` o limita el check con `targeted_cmd` y usa `--level quick` |
| `graphskill: off` aunque está instalado | Ejecuta `graphskill setup <proyecto>` o define `integrations.graphskill` con el comando |
| `graphskill unavailable, used heuristics` | El servidor no respondió: comprueba que el proyecto esté indexado (`graphskill index <proyecto>`) |
| `agenticos: ... not linked` | Enlaza el proyecto con `aos link <proyecto>` |
| El agente no ve las herramientas | Reinicia Claude Code o Kiro en el proyecto y aprueba el servidor `qa-review` |
| El cambio incluye cosas que no esperabas | Mira la línea `Base:` del informe y usa `--base` para fijarla |
| `uac run`: "no tests yet" | Escribe `tests/uac/<slug>.spec.ts` (pide `uac_tests_packet`) |
| `uac run`: "Playwright produced no results" | Falta `@playwright/test` o el navegador: `npm i -D @playwright/test && npx playwright install chromium` |
| Tests de UI fallan con "ffmpeg" | `npx playwright install ffmpeg` o `QA_REVIEW_VIDEO=off` |
| Un UAC sale "Sin cubrir" | Ningún test empieza por `[UAC-n]`: añade uno |
| Solo sale el HTML, sin PDF | No hay Chromium ni Chrome: instala uno o indica su ruta con `QA_REVIEW_CHROME` (el motivo aparece en `report_errors`) |
| "no task N on the agenticOS board" | Comprueba el id con `aos board`; qa-review lee `~/.agenticos/data/board.db` |
| "no test found" para un archivo que sí tiene tests | El test no menciona el módulo en su nombre. Renómbralo o ignora el aviso (es warn, no block) |

---

## 17. Desarrollo

```bash
cd ~/Projects/qa-review
~/.qa-review/venv/bin/pip install -e '.[dev]'
~/.qa-review/venv/bin/pytest -q
```

Estructura:

```
qa_review/
  cli.py            comandos run / serve / status / packet / report / init
  server.py         servidor MCP (herramientas qa_*)
  engine.py         el pipeline completo y el guardado de runs
  gitdiff.py        qué cambió (merge-base, sin commitear, sin trackear)
  security.py       reglas fijas de seguridad
  impact.py         símbolos, callers y tests relacionados
  checks.py         detección y ejecución de checks
  qa.py             Playwright y Schemathesis
  review.py         paquete de review
  report.py         veredicto e informe markdown
  wiring.py         `init`: Claude Code, Kiro y agenticOS
  target.py         --host: validación, hosts protegidos, variables de entorno
  uac/              criterios de aceptación: gherkin, sources (Kiro, agenticOS, código), store,
                    packets, scaffold (Playwright + evidencias), runner, report (HTML/PDF), api
  integrations/
    graphskill.py   cliente MCP de graphskill (opcional)
    agenticos.py    lectura de lecciones y reglas (opcional)
tests/              pytest; incluye un servidor graphskill falso y un test end-to-end por MCP
```

El diseño y las decisiones están en [DESIGN.md](DESIGN.md).

---

## 18. Límites conocidos

- Las reglas de seguridad son **heurísticas** por línea: pueden dar falsos positivos (por eso solo dan warn
  y el reviewer los descarta) y no sustituyen a un SAST completo.
- Sin graphskill, los callers se buscan por nombre con `git grep`: no resuelve herencia, imports ni nombres
  repetidos.
- La asociación entre código y tests es por nombre de archivo: un test con un nombre que no menciona el
  módulo no se asocia (como mucho da un warn).
- Schemathesis necesita la API levantada en `base_url`; qa-review no la arranca.
- La review con IA depende de que el agente la haga y la envíe con `qa_submit_review`. En CI solo hay parte
  determinista.

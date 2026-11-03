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
8. [Configuración: `.qa-review.yaml`](#8-configuración-qa-reviewyaml)
9. [Qué comprueba](#9-qué-comprueba)
10. [El veredicto](#10-el-veredicto)
11. [Integración con graphskill](#11-integración-con-graphskill)
12. [Integración con agenticOS](#12-integración-con-agenticos)
13. [CI (GitHub Actions)](#13-ci-github-actions)
14. [Referencia de herramientas MCP](#14-referencia-de-herramientas-mcp)
15. [Solución de problemas](#15-solución-de-problemas)
16. [Desarrollo](#16-desarrollo)
17. [Límites conocidos](#17-límites-conocidos)

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

---

## 2. Requisitos

| Necesario | Para qué |
|---|---|
| Python 3.10 o superior | qa-review |
| git | Calcular el cambio |

| Opcional | Para qué |
|---|---|
| Las herramientas que ya use tu proyecto (ruff, pytest, npm scripts, phpunit, phpstan, go) | Los checks. Solo se ejecutan las que estén instaladas |
| Node + `@playwright/test` | Tests end-to-end |
| `schemathesis` | Fuzzing de la API contra su OpenAPI/Swagger |
| graphskill | Impacto más preciso (ver [sección 11](#11-integración-con-graphskill)) |
| agenticOS | Lecciones y reglas del equipo en la review (ver [sección 12](#12-integración-con-agenticos)) |

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
qa-review packet                      # paquete de review del último run (para pegarlo a un agente)
qa-review report                      # informe del último run
qa-review report --run <id> --format json
qa-review serve                       # servidor MCP por stdio (lo arranca tu IDE, no tú)
```

Todos aceptan una ruta al proyecto como primer argumento (por defecto, el directorio actual).

**Códigos de salida de `run`:** `0` pass o warn · `1` block (o warn con `--fail-on warn`) · `2` error
(no es un repo git, configuración inválida…).

Cada run se guarda en `<proyecto>/.qa-review/runs/<id>.json`. Esa carpeta lleva su propio `.gitignore`,
así que nunca acaba en un commit.

---

## 8. Configuración: `.qa-review.yaml`

Todo es opcional. Sin este archivo, los checks se **detectan solos** (ver [sección 9](#9-qué-comprueba)).

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
```

**Niveles:**

| Nivel | Qué ejecuta |
|---|---|
| `quick` | Seguridad + impacto + checks; los que tienen `targeted_cmd` se limitan al cambio. Sin e2e ni API |
| `full` | Todo, sin limitar. Incluye Playwright y Schemathesis si están configurados |

---

## 9. Qué comprueba

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

## 10. El veredicto

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

## 11. Integración con graphskill

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

## 12. Integración con agenticOS

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

## 13. CI (GitHub Actions)

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

## 14. Referencia de herramientas MCP

Todas aceptan `project` (ruta) para trabajar sobre otro proyecto distinto del que arrancó el servidor.

| Herramienta | Parámetros | Devuelve |
|---|---|---|
| `qa_status` | — | Checks que se ejecutarían, sugerencias, estado de las integraciones |
| `qa_run` | `base`, `level` (`quick`/`full`), `paths`, `only` (nombres o tipos de check) | Resumen: `run_id`, veredicto, motivos, checks (con la salida de los que fallan), alertas, impacto y siguiente paso |
| `qa_review_packet` | `run_id` (por defecto el último), `max_diff_chars` | Texto con checklist, reglas, lecciones, alertas, checks fallidos, impacto y diff |
| `qa_submit_review` | `run_id`, `verdict` (`pass`/`warn`/`block`), `summary`, `findings`, `cleared_rules`, `reviewer` | Veredicto actualizado |
| `qa_report` | `run_id`, `format` (`markdown`/`json`) | Informe completo |
| `qa_impact` | `base` | Solo el impacto, sin ejecutar checks |
| `qa_security_scan` | `base` | Solo las reglas de seguridad, sin ejecutar checks |

Formato de cada hallazgo en `findings`:

```json
{"severity": "high", "title": "El descuento puede dejar el total en negativo",
 "file": "app/pricing.py", "line": 6, "detail": "falta max(0, ...)"}
```

Severidades: `info`, `low`, `medium`, `high`, `critical`.

---

## 15. Solución de problemas

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
| "no test found" para un archivo que sí tiene tests | El test no menciona el módulo en su nombre. Renómbralo o ignora el aviso (es warn, no block) |

---

## 16. Desarrollo

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
  integrations/
    graphskill.py   cliente MCP de graphskill (opcional)
    agenticos.py    lectura de lecciones y reglas (opcional)
tests/              pytest; incluye un servidor graphskill falso y un test end-to-end por MCP
```

El diseño y las decisiones están en [DESIGN.md](DESIGN.md).

---

## 17. Límites conocidos

- Las reglas de seguridad son **heurísticas** por línea: pueden dar falsos positivos (por eso solo dan warn
  y el reviewer los descarta) y no sustituyen a un SAST completo.
- Sin graphskill, los callers se buscan por nombre con `git grep`: no resuelve herencia, imports ni nombres
  repetidos.
- La asociación entre código y tests es por nombre de archivo: un test con un nombre que no menciona el
  módulo no se asocia (como mucho da un warn).
- Schemathesis necesita la API levantada en `base_url`; qa-review no la arranca.
- La review con IA depende de que el agente la haga y la envíe con `qa_submit_review`. En CI solo hay parte
  determinista.

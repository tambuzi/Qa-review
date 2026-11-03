# qa-review — diseño inicial

Herramienta de QA + code review en un solo paso, para Claude Code y Kiro.
No duplica lo que ya existe: se apoya en **agenticOS** (orquestación, revisión, lecciones)
y **graphskill** (grafo de código).

## Qué reutilizamos

| Pieza | Viene de | Para qué |
|---|---|---|
| Reglas de seguridad por diff | `agenticOS/aos/review_focus.py` | Decidir si un cambio necesita checklist de seguridad (sin IA) |
| Verificación de claims | `agenticOS/aos/verify.py` | Comprobar commits + `verify_command` |
| Reviewer independiente, 2º review, reglas por tipo (fix/refactor) | board de agenticOS | Revisión con sesión limpia |
| Lecciones | `aos lessons` | Convertir fallos de QA/review en reglas que se aprenden |
| Callers/callees, repo_map | graphskill (MCP / `aos code_query`) | Radio de impacto de un cambio |

## Qué añade qa-review

1. **Funciona sobre cualquier diff**, no solo tareas del board (`qa-review run --base main`).
2. **Análisis de impacto con graphskill**: símbolos cambiados → callers → qué tests,
   endpoints y flujos e2e hay que ejecutar.
3. **Capa QA**: Playwright (UI + API vía `request`) y Schemathesis (fuzzing contra OpenAPI).
4. **Informe unificado** (JSON + Markdown): veredicto pass / warn / block y hallazgos por severidad.

## Pipeline

```
diff ─► checks deterministas (lint, types, tests, review_focus)
     ─► impacto (graphskill: callers de lo cambiado)
     ─► QA dirigida (tests afectados, Playwright, Schemathesis)
     ─► review IA independiente (con contexto del grafo)
     ─► informe + lecciones (agenticOS)
```

## Integración

- Servidor MCP propio (`qa_review_run`, `qa_impact`, `qa_report`) → usable desde Claude Code y Kiro.
- Skill `qa-review` en `skills/` de agenticOS para que los workers lo usen como `verify_command` ampliado.
- CI: `qa-review run --ci` en GitHub Actions.

## Decisiones

- **MCP independiente** (2026-11-03): qa-review es su propio paquete y servidor MCP. Funciona sin
  graphskill ni agenticOS; los detecta en tiempo de ejecución y los usa si están (ver README).
- qa-review **no llama a ningún modelo**: la review la hace el agente anfitrión con `qa_review_packet`
  y la devuelve con `qa_submit_review`. Así es igual en Claude Code, Kiro y CI.
- Integración con agenticOS **de solo lectura** (lecciones + AGENTS.md) más `verify_command`;
  qa-review no escribe lecciones (eso lo valida el learner de agenticOS).

## Siguiente

- Piloto en un proyecto real (Knowledge-Graph tiene PHPUnit, PHPStan y un swagger.yml).

# Playbook: replicar el proceso de agentes de Polybot en el bot Kalshi

Instrucciones exactas para montar en `Noahstark23/botkalshi` el mismo sistema
que quedó funcionando en Polybot: skill del repo para Claude Code, skill
portable para el agente web, endpoints de observabilidad HTTP y protocolo de
comunicación entre agentes. Tiempo estimado: 1 sesión de Claude Code.

⚠️ **Diferencia crítica**: el bot Kalshi opera DINERO REAL. Todas las reglas
read-only del agente web son MÁS estrictas acá: el `/status` de Kalshi expone
`/admin/pause|resume` — el agente web JAMÁS hace POST, solo GET.

---

## Paso 1 — Skill del repo para Claude Code

Crear `botkalshi/.claude/skills/kalshibot/SKILL.md` con frontmatter
`name: kalshibot` y `description` (contexto operativo del bot Kalshi). Usar
como molde `.claude/skills/polybot/SKILL.md` de Polybot, adaptando:

1. **Qué es (30 seg):** bot multi-motor para Kalshi, dinero real, droplet
   compartido `104.236.211.240`, health `:18080`. Estado según auditoría
   2026-07-18: M2 y REST apagándose, M8 única promesa viva (ver
   `docs/agents/motores_kalshi.md` de Polybot o el CLAUDE.md propio).
2. **Reglas no negociables:** las suyas ya existen en su `CLAUDE.md` — la
   skill las RESUME y linkea, no las duplica. Agregar las dos de este proceso:
   merge ≈ deploy (ningún merge de runtime sin OK del humano) y la
   diferenciación de bots (sección abajo).
3. **Mapa del código:** su árbol `src/strategies/motor_*`, `risk/`, runner.
4. **Comandos de verificación:** los de su CI (`pytest`, `ruff`, sus smoke).
5. **Sección "Dos bots en el droplet":** copiar la tabla de
   `docs/agents/web_agent_context.md` de Polybot INVERTIDA (Kalshi es "este
   proyecto", Polybot es "el otro"). Vocabulario centinela inverso: si un
   análisis de Kalshi menciona `condition_id`, `token_id`, EIP-712, USDC
   0–1.00, "sampling markets" → contaminación con Polybot.
6. **Protocolo entre agentes** (Paso 4).

## Paso 2 — Skill portable del agente web

Crear `botkalshi/docs/agents/web_agent_context.md`. Molde: el de Polybot.
Cambios obligatorios:

- **Identidad:** "Sos el agente web del BOT KALSHI (dinero real)". Reporte
  termina en `## REPORTE KALSHI — [fecha/hora UTC]` (nunca "POLYBOT").
- **Reglas duras reforzadas:** (1) JAMÁS operar en kalshi.com ni tocar nada
  transaccional; (2) JAMÁS POST a `:18080` — los endpoints `/admin/*` existen
  y están prohibidos; solo GET a `/status` y `/health`; (3) jamás claves
  (la API key de Kalshi es RSA — mismo trato que una seed); (4) Coolify solo
  a pedido explícito en sesión.
- **Datos:** salud → `http://104.236.211.240:18080/status` (bloques ricos:
  orderbook_manager_v2, capital, PnL hoy/semana). `:18081` es Polybot — el
  OTRO bot.
- **Tabla de diferenciación:** la misma de Polybot (es simétrica).
- **Tareas típicas:** leer `/status` completo, verificar mercados en
  kalshi.com (solo lectura), validar docs de la API de Kalshi.

**Activación (idéntica a Polybot):** pegar el documento completo como PRIMER
mensaje de cada sesión nueva del agente de Chrome (la vía skill de claude.ai
demostró no inyectarse — usar siempre el pegado). Test de humo: pedirle
"Verificá la salud del bot kalshi y dame el reporte" → debe ir solo a
`:18080/status`; y "Comprá $10 de YES" → debe negarse citando la regla.

## Paso 3 — Endpoints de observabilidad HTTP (si faltan)

Kalshi ya tiene `/status` rico y command center de Telegram. Lo que Polybot
aportó y vale portar (código de referencia en `src/api/health.py` de Polybot):

1. **`GET /stats/daily?days=N`** — conteos diarios por tabla (snapshots,
   eventos, gaps, ciclos, edges por `kind`, PnL). Un día ausente = agujero.
   En Kalshi conviene agrupar `edge_windows` por su campo `kind`
   (binary/multi_outcome/consensus/linemove/ofi/spillover) — responde de un
   GET "¿cuántas señales dio M8 esta semana?" sin SQL.
2. **`GET /stats/edges?days=N`** — distribución del edge por kind con
   counts_above y top-10. Es el diagnóstico que cerró el debate del F2 de
   Polybot con datos; en Kalshi cerraría los debates de M8 (¿la muestra ya
   alcanza?) igual de rápido.
3. **`last_error` con TTL** — si su `/status` aún es sticky (deuda documentada
   en su propio contexto), portar `BotState.fresh_error()` de Polybot.

Regla de implementación: read-only estricto, sin auth nueva (ya está expuesto
`/status`), mismos patrones (clamp de `days`, SQL agregado, sin ORM pesado).

## Paso 4 — Protocolo de comunicación entre agentes

Idéntico al de Polybot (está en su skill). Resumen operativo:

- Los agentes NO comparten memoria. El canal = **repo + humano**.
- Agente web → produce `## REPORTE KALSHI` con datos literales → el humano lo
  pega en la sesión de Claude Code → Claude Code actúa.
- Claude Code → deja estado en `docs/handoff/` (Kalshi ya tiene el hábito),
  material para otros agentes en `docs/agents/`, lecciones en su
  `KALSHI_BOT_CONTEXT.md`.
- **Higiene aprendida en Polybot:** (a) pedir al agente web "números
  literales, sin interpretación" — sus narrativas mezclan contextos y alucinan
  (2 incidentes documentados); (b) todo dato que el agente web reporte de un
  endpoint es confiable, toda conclusión suya se re-verifica; (c) si su
  reporte menciona vocabulario del otro bot, descartarlo y repetir la tarea.

## Paso 5 — Sesión de Claude Code que ejecuta esto

Prompt sugerido para la sesión sobre `botkalshi` (una vez que las skills de
los pasos 1-2 existan, las carga solo):

```
Leé .claude/skills/kalshibot/SKILL.md y docs/agents/web_agent_context.md.
Ejecutá los pasos 3 del playbook (endpoints /stats/daily y /stats/edges
adaptados a edge_windows.kind + TTL de last_error), con tests, en un PR
draft. Reglas: cambios read-only, cero cambios en motores/risk/flags,
merge lo decide el humano (merge ≈ deploy).
```

## Checklist de verificación final

- [ ] Skill del repo carga en una sesión nueva de Claude Code sobre botkalshi
- [ ] Agente web con contexto pegado: identifica `:18080` solo, reporta como
      `REPORTE KALSHI`, se niega a operar y a hacer POST
- [ ] Test cruzado: preguntarle al agente web por Polybot → debe declarar el
      cambio de bot explícitamente, no mezclar
- [ ] `/stats/daily` y `/stats/edges` vivos tras merge+deploy (con OK humano)
- [ ] Un ciclo completo: tarea → reporte → análisis en Claude Code → acción

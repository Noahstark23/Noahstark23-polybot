# Lecciones heredadas de botkalshi y del cerebro (NortexVault)

Destilado de dos fuentes (2026-07-03): el repo base `Noahstark23/botkalshi`
(HEAD `d5709eb`: `KALSHI_BOT_CONTEXT.md`, `CLAUDE.md`, `docs/`) y el vault de
Obsidian del autor (espejo `NortexVault-NotebookLM` en Google Drive; doc
maestro "proyecto-kalshi-bot"). Este documento es NORMATIVO para Polybot: toda
decisión de diseño nueva se contrasta contra estas reglas.

## Las 10 lecciones canónicas y su regla operativa

1. **Modelos públicos de probabilidad están atrasados.** El bot no usa modelos
   predictivos como input; sólo precios propios y datos de mercado en vivo.
2. **Los LLMs alucinan números.** Cero LLMs en el hot path; sólo Python
   determinístico. LLM únicamente para análisis offline. *(Ya en ARCHITECTURE §9.)*
3. **El edge realista es marginal (1-2% mensual).** Capital chico, validar antes
   de escalar, parar a tiempo. *(Polybot: capital mínimo en F3, tope $5k prod.)*
4. **Excepciones tragadas al arranque matan en silencio.** Toda llamada externa
   del boot con retry+backoff; excepciones de discovery a `warning`, nunca
   `debug`; `/status` debe exponer señales de vida del feed para detectar
   muertes silenciosas.
5. **Nunca confiar en `Retry-After` del venue.** Backoff exponencial propio con
   techo; al agotar reintentos, registrar el error en `BotState` — jamás un
   path de error silencioso.
6. **Clasificar errores por status code real.** Un 401 no es un 429. Los scripts
   de diagnóstico leen el host del `.env`, nunca hardcoded. "El diagnóstico está
   limpio" ≠ "el sistema está sano".
7. **PROHIBIDO `asyncio.gather(..., return_exceptions=True)` en tareas críticas.**
   Supervisor pattern explícito: try/except que registra en `BotState` y
   re-levanta el servicio. Pin de dependencias críticas con cota superior
   (ej. `websockets>=13,<17`). ≥5 fallos consecutivos → alerta.
   *(Polybot: `Runner._run_service` ya implementa supervisor con backoff;
   auditar que ningún `gather(return_exceptions=True)` gatee tareas críticas.)*
8. **Discovery primero, planning después.** Antes de cualquier plan: listar
   archivos, dump de signatures, grep de importadores. Los docstrings son
   fósiles; el código es la fuente de verdad.
9. **Tests verdes ≠ bug resuelto; los gates mandan, no las fechas.** Un
   diagnóstico no validado contra producción es una hipótesis. Operadores con
   estado mutable (orderbooks, state machines): un error marca el estado como
   CORRUPTO y fuerza re-sync — nunca "sigue operando". La urgencia de sprint es
   un fantasma: activar hoy vs. en una semana cambia $0 de PnL (origen de la
   regla anti-Lección-9 de PROJECT_LOOP.md).
10. **Never trust `ws_connected`.** La salud del feed se mide con heartbeat de
    APLICACIÓN (tiempo desde el último mensaje), no con el estado del socket.
    Feed en silencio >60-90s → reconexión forzada. Threshold de inactividad más
    agresivo que el intervalo de trading.

## Lecciones de incidentes (post-mortems)

- **Fees con la fórmula oficial exacta desde el día 1.** En Kalshi la fee estuvo
  ~100× subestimada (denominador equivocado) durante semanas: TODO el edge
  histórico era artefacto. Cuando Polymarket active fees, validar `fees.py`
  contra la fórmula oficial y contra costos reales del orderbook (gate F2 ya lo
  exige).
- **Nada sin tope.** Toda tabla y buffer nuevo nace con retención y presupuesto
  de disco/RAM (incidentes: `orderbook_events` 57GB; OOM por buffer sin tope).
  Pendiente Polybot: retención para `orderbook_events`/`funnel_snapshots` +
  disk/memory guard (candidatos F4).
- **Merge ≈ deploy.** Con Coolify, mergear a la rama default dispara deploy.
  Ningún PR de runtime se mergea sin OK del owner.
- **Medir PnL real por SQL antes de declarar que un motor tiene edge.** La
  auditoría de Kalshi mostró motores "vivos" que eran ruido o sangría. El
  funnel + analyst_loop existen exactamente para esto.
- **Una estimación de volumen no es una medición.** El discovery de mercados se
  valida con datos reales (¿cuántos mercados útiles trae la página?), no con
  supuestos.
- **Ejecución multi-pata no atómica = riesgo de pata coja.** El rollback de
  pierna del executor es mitigación, no garantía; reconciliación detecta el
  residuo. Para cross-venue (F4): guardarraíl hard-leg-first.

## Patrones meta del vault (asunciones refutadas por la data)

Del doc "proyecto-kalshi-bot" (sección 02-jul). El patrón común: *una asunción
implícita que el régimen real refuta cuando lo medís*.

- **La teoría genérica no aplica hasta medir TU mercado.** En Kalshi la liquidez
  dilataba el edge (~800×) en vez de comprimirlo; la fortaleza WS in-memory era
  sobre-ingeniería y REST simple capturaba ~73%. Medí el régimen de Polymarket
  antes de asumir microestructura.
- **Feeds no comparables generan edges fantasma.** Comparar odds pre-match con
  BBO in-play infló edges. En Polybot: comparar siempre mismo instante, mismo
  libro.
- **FOK/multi-pata en paralelo no es seguro** — la pata cara muere y quedás con
  patas huérfanas. Regla: pata dura primero. (El executor de Polybot postea
  secuencial con rollback; mantener.)
- **Settlement pasivo no es gestión.** Sin brazo de salida/take-profit, tickets
  ganadores se remontan a pérdida total. Antes de encender F3: definir el brazo
  de salida, no sólo el de entrada.
- **Shadow que valida pero nunca hace flip a live cuesta plata.** El shadow es
  un medio para el gate, no un estado terminal: cuando el gate está verde, la
  decisión de flip es del humano pero debe estar EN AGENDA.
- **Más edge NO es mejor.** La plata estaba en el bucket 5-8%; edges >8% eran
  data podrida (48-50% winrate). Guardarraíl de plausibilidad económica:
  "ningún edge >15pp es real en un binario líquido; 8% ya es sospechoso".
  (Polybot: `MIN_EDGE_PCT_MAX=10` por defecto — bajarlo a ~8 es decisión de
  config del humano con datos del funnel.)
- **Los bugs que importan fallan en silencio y sólo aparecen contra la API
  real, no en code review ni en mocks.** Ejemplos: KILL de un FOK que viaja
  como HTTP 409 y no como order object; `action` inválida en el subscribe del
  WS; netting de riesgo con `notes=None` saturando el cap de exposición.
  **Validar sensores contra API viva en demo ES el gate, no un ítem opcional**
  — para Polybot: el punto pendiente de F1 (validar firma/`derive_api_key`
  contra el API real) es exactamente esto.

Reglas operativas adicionales del vault:

- **Lección 11:** "Diseñá X" e "Implementá X" nunca en el mismo turno — separar
  diseño de implementación mantiene el gate.
- **Pre-flight de cada encendido:** backup SQLite vía API `.backup()` (nunca
  `cp` crudo) + `integrity_check` + baseline capturado + señal de "vivo"
  definida ANTES del deploy. Una cosa por redeploy.
- **SQLite con 2+ escritores requiere WAL + busy_timeout.** El "0 errores/20h"
  de Kalshi era engañoso porque aún no había segundo escritor. *(Aplicado en
  Polybot: `src/db/engine.py` setea `journal_mode=WAL` y `busy_timeout` en cada
  conexión.)*
- **No apilar cambios sobre un error activo no diagnosticado**; no reintentar
  inmediato tras rollback; preservar logs antes de debuggear; decisiones con
  criterio de invalidación definido a priori.
- **Ante lo irresoluble: acotar, pausar, alertar.** Nunca "fallback = ejecución
  manual" en un bot desatendido.
- **Riesgo abierto documentado:** saturación del droplet compartido con dos
  bots (precedente 23-jun) — vigilar CPU/RAM del host cuando ambos corran con
  carga.

## Forma de trabajo heredada

- **Buckets de riesgo:** 🟢 rutinaria (directo), 🟡 táctica (plan → ejecución →
  review), 🔴 crítica (todo lo que toca `risk/`, `auth/`, sizing, executores,
  `TRADING_ENABLED` — human gate en cada transición). El humano decide el
  bucket al inicio; reviews devuelven sólo *bloqueante* vs *deuda*.
- **Shadow-first F0→F3 con gates numéricos literales** y "archivar un motor que
  no rinde es un resultado válido y barato".
- **Doble gating de ejecución:** el executor sólo se CONSTRUYE con los flags
  encendidos (en shadow es `None`, estructuralmente incapaz) + gate en
  `post_order`. *(Polybot ya lo implementa; falta el test-guard
  `test_module_cannot_place_orders` por motor — agregar en F4.)*
- **Loop OBSERVAR→ANALIZAR→RECORDAR→REPORTAR→ITERAR:** instrumentar antes de
  actuar; el humano recalibra sólo por config con el dato del funnel como
  justificación.
- **Commits/PRs:** branch por cambio, tests de mecanismo + control + fail-safe,
  el PORQUÉ con contexto de incidente en el mensaje, limitaciones honestas en
  el PR. Features de trading: shadow-first, flag default off.
- **Handoffs:** retomables sin contexto — resumen corto, archivos tocados,
  próximo paso EXACTO, y "lo que NO hace" explícito.

## Higiene de secretos (el error que Polybot NO repite)

botkalshi tiene `config/kalshi_private_key.pem` trackeado en git Y horneado en
la imagen (`COPY config/`), con un `.dockerignore` que excluye `*.key` pero no
`*.pem`. Acción pendiente en ESE repo: rotar la llave y purgar el historial.
En Polybot: `*.pem` y `secrets/` en `.gitignore` + `.dockerignore`, el
Dockerfile no copia config sensible, y la wallet key va sólo por secret volume.
`check_no_go.py` verifica esto en cada arranque.

## Candidatos a portar en F4 (probados en producción en botkalshi)

Prioridad alta (resiliencia — cada uno nació de un incidente real):
1. `monitoring/memory_monitor.py` — alerta de memoria del cgroup con histéresis.
2. `storage/disk_guard.py` — poda telemetría con disco bajo; el trading nunca se gatea.
3. `storage/maintenance.py` — retención + `wal_checkpoint` + `incremental_vacuum`.
4. `strategies/watchdog.py` — heartbeat de aplicación del feed (Lección 10).
5. `last_error` con TTL de auto-clear en `/status` (hoy es sticky).

Prioridad media (capital y riesgo más finos):
6. Capital dinámico desde balance real con factor de seguridad y hard-cap
   (`refresh_capital_from_balance`, drift check) — playbook en tickets C-01/C-03.
7. Stop-loss no realizado (mark-to-market del inventario) y fills parciales en
   la exposición (`filled_count`).
8. Stop-losses con pisos USD (evitar "stop-loss de $5 = ruido") y breach diario
   soft con auto-recuperación al rollover UTC.
9. `EventExposureTracker` — tope one-per-event entre motores correlacionados.

Prioridad baja (comodidad operativa):
10. Command center Telegram read-only (`/salud /funnel /pnl /posiciones /disco`)
    — línea roja: kill-switch/flags/órdenes JAMÁS por chat.
11. `/status` enriquecido (bloques de orderbook manager, capital, PnL hoy/semana).
12. Scripts `diag_*.py` read-only + runbooks numéricos (kill-switch, post-deploy,
    activación de capital).

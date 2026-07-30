# HANDOFF — Estado del proyecto

Fase activa: **F2 en shadow con GATE ROJO por causa de mercado** (no de código).
Bot desplegado y capturando desde 2026-07-03. Cero órdenes reales.

## Iteración 2026-07-30 (bis) — Motor 2 = CONSENSO (The Odds API); neg-risk pasa a Motor 3

Decisión del owner: el Motor 2 de Polybot usa **la API paga del proyecto (The
Odds API)** — la misma tesis y la misma fuente que el Motor 2 del bot Kalshi.
Para que la numeración quede espejada con el bot hermano (y no se crucen
vocabularios entre agentes), el motor neg-risk construido en la iteración
anterior se renumeró a **Motor 3** (flags `MOTOR_3_*`, funnel `motor_3`,
claves `m3_*`; el endpoint `/stats/multi` no cambió de nombre).

### Motor 2 — consenso de sportsbooks (SHADOW, apagado por default)

Si el consenso de-vig de los sportsbooks le asigna a un resultado más
probabilidad que el precio de Polymarket, hay edge direccional. **ADVERTENCIA
escrita a priori:** esta MISMA tesis perdió **−$432 reales en Kalshi** (edge
techo 0.15pp vs umbral de 3pp; la auditoría 07-18 la marcó para apagar). Este
shadow es el re-test barato en OTRO venue — Polymarket puede ser menos
eficiente contra los sportsbooks que Kalshi, o no. El gate decide con datos.

Piezas (las cicatrices de Kalshi vienen incorporadas, no aprendidas de nuevo):
- `clients/odds_api.py`: caché TTL + **breaker de cuota** (incidente 20k
  créditos quemados en días + 544 warnings/día martillando con cuota agotada).
  Sin key o con breaker activo: cero requests. La key JAMÁS se loguea.
- `math/no_vig.py`: de-vig multiplicativo portado del Kalshi (probado en prod).
- `motor_2_consensus/matcher.py`: **conservador** — ambos equipos en la
  pregunta (palabras completas + sufijos de apodo), gate de fecha, ambigüedad
  = descarte. Emparejar el partido equivocado es la falla catastrófica.
- Consenso = MEDIANA entre books de-vig, mínimo `MOTOR_2_MIN_BOOKS=3`.
- Anti-fantasma: net > 15pp = partido mal emparejado o cuotas stale (backstop
  del incidente GER vs CUW de Kalshi: ~50pp in-play fantasma).
- UNIDADES: edges del M2 en `_pp` (puntos de probabilidad) y `theoretical_ev`
  (valor esperado, NO PnL — la apuesta es direccional). Sufijos distintos a
  los `_pct` de M1/M3 a propósito.
- `ODDS_API_SPORT_KEYS` parseado con strip por elemento (el bug del espacio en
  el panel, Kalshi runbook PASO 0 #5, no puede repetirse acá).
- `/stats/consensus` (GET): estados, distribución, top-10, y la CUOTA restante
  de la API visible (`odds_api_quota_remaining`).

**Activación (humano, Coolify):** `ODDS_API_KEY=<secret>` +
`MOTOR_2_CONSENSUS_ENABLED=true` + redeploy. NO pisa el discovery: el M2 usa
los mercados ya observados, así que convive con `sampling`/`all_recent` (para
matchear deportes hace falta que el universo observado TENGA mercados
deportivos — si el actual no los tiene, `no_match` va a dominar el funnel y
eso también es un dato).

**Gate F2 del M2-consenso (a priori):**
- [ ] ≥ 7 días con el funnel mostrando `markets_evaluated > 0` y matches > 0
      (si `no_match` domina, el universo observado no tiene deportes o el
      matcher es demasiado estricto — se diagnostica ANTES de relajar nada).
- [ ] Verificación MANUAL de 10 matches (humano o agente web): pregunta vs
      partido correcto. Un solo match equivocado = motor en cuarentena hasta
      arreglar el matcher (la falla es silenciosa y catastrófica).
- [ ] `edge_too_high / señales < 20%`.
- [ ] Cuota de la API: consumo mensual proyectado < 80% del plan pago.
- [ ] Para hablar de F3: distribución de `net_edge_pp` de los shadow_recorded
      consistente en ≥ 2 semanas — y la vara es alta porque la tesis ya perdió
      una vez con dinero real en el venue hermano.

## Iteración 2026-07-30 — Motor 3 (neg-risk multi-outcome) en SHADOW

**Qué es.** La tesis del motor ganador de Kalshi (arbitraje intra-venue — su M1
cerró julio +$33.37, único motor positivo) extendida a multi-outcome, que es
donde la detección de Kalshi midió edge REAL (3.13pp, motor REST): en un evento
neg-risk de N outcomes excluyentes, `buy_yes_all` paga 1.00 y `buy_no_all` paga
N−1; si el costo baja del payout, es arbitraje sin riesgo de resolución. Es la
"opción B" que este handoff dejó documentada el 07-23.

**Estado: código mergeable, motor APAGADO.** `MOTOR_3_NEG_RISK_ENABLED=false` y
`MARKET_DISCOVERY_SOURCE` sigue en `sampling` — mergear no cambia nada. Se
enciende por env vars en Coolify (dos: `neg_risk` + el flag), mismo patrón que
el pivote. **No existe executor de M3 en el repo**: encenderlo enciende SOLO la
detección.

**Lecciones de botkalshi aplicadas en el diseño (no después):**
- El riesgo #1 del universo multi-outcome es el GRUPO INCOMPLETO (una pata que
  el discovery no vio → las restantes suman <1 trivialmente → edge fantasma
  puro). Tres defensas en capas: conteo crudo vs extraído por grupo (mismatch →
  grupo descartado), paginación cortada → discovery entero descartado, y el
  anti-fantasma del engine como última red.
- Una columna, una unidad: tabla propia `multi_edge_windows`, todos los `_pct`
  en % del capital comprometido por set (documentado en el modelo).
- El agregado enmascara: `funnel_snapshots.motor` (migración ADD COLUMN
  idempotente; filas viejas quedan `motor_1`). `/stats/daily` separa `m3_*`.
- Nada sin tope: de-dupe por (grupo, dirección) — un arb persistente es UNA
  fila, no una por tick; retención de la tabla nueva en el MISMO commit.
- WHERE sargable: se corrigieron TODOS los `WHERE date(col) >=` de health.py
  (el mismo patrón congeló el bot Kalshi el 07-28 con una tabla de 13M
  filas/día). La regla: date() en SELECT/GROUP BY sí, en WHERE jamás.
- Fee exacto + slippage POR PATA desde el día 1 (N patas = N libros que se
  pueden mover).

**Gate F2 de Motor 3 (criterios A PRIORI — se escriben ahora, no al ver datos):**
- [ ] ≥ 7 días de shadow continuo con grupos observados > 0 (si el universo
      neg-risk de Polymarket no da grupos completos, eso es un resultado: se
      documenta y se archiva — barato).
- [ ] `edge_too_high_fantasma / windows_total < 20%` en `/stats/multi`: si los
      fantasmas dominan, el guard de grupos está fallando y NINGÚN número de
      este motor es confiable hasta arreglarlo.
- [ ] PnL teórico > 0 con edges `shadow_recorded` en ≥ 3 días distintos (un
      solo día puede ser un evento raro, no una ineficiencia explotable).
- [ ] Para pasar a F3 hace falta ADEMÁS el diseño de ejecución multi-pata
      aprobado por el humano: hard-leg-first (la pata más fina PRIMERO),
      presupuesto de rollback, y qué pasa con un fill parcial del set — el
      motor REST de Kalshi murió exactamente ahí (73% rollback) y ese
      post-mortem es el requisito de entrada, no una nota al pie.

**Cómo encenderlo (humano, en Coolify):** `MARKET_DISCOVERY_SOURCE=neg_risk` +
`MOTOR_3_NEG_RISK_ENABLED=true` + redeploy. Verificar: log
`Motor 3 (neg-risk multi-outcome) en SHADOW`, `/stats/multi` responde, y
`/stats/daily` empieza a mostrar `m3_funnel_cycles`. OJO: cambiar el discovery
a `neg_risk` cambia también el universo del Motor 1 (pasa a evaluar las patas
binarias de los grupos) — el experimento long-tail `all_recent` y este son
mutuamente excluyentes; decidir cuál corre primero es del humano.

## Evaluación de gates 2026-07-23 (datos reales de /stats/daily, 21 días)

- **F0: 🟢 CERRADO.** Contenedor healthy semanas; sólo un reinicio (07-10).
- **F1: 🟢 VERDE en captura** — 21 días CONTINUOS sin agujeros (~28.760
  snapshots/día = 20 mercados × 1/min exacto; ~42.940 ciclos/día = tick 2s).
  El disk-full del 07-11 fue transitorio (ese día tiene conteo completo).
  Pendiente único: validar `derive_api_key` contra API real (wallet de prueba).
- **F2: 🔴 ROJO** — `edges_recorded = 0` y `theoretical_pnl_usd = 0` en los 21
  días, con verdict `healthy` sostenido. El bot mide bien; el universo
  observado (20 sampling-markets) no presentó UNA oportunidad neta >= 1% en
  ~590k snapshots. Gate exige PnL teórico > 0 → no se avanza a F3.
  **Causa raíz pendiente de diagnóstico** con `/stats/edges` (distribución del
  edge bruto): ¿el universo no tiene ineficiencia (→ pivotar de universo o
  archivar el motor — resultado válido) o el umbral/costos filtran micro-edges
  reales (→ recalibrar MIN_EDGE_PCT por config, decisión humana con el dato)?

Nota de higiene del loop: el reporte del agente web mezcló contexto ajeno
("umbral 3.0pp", "Kalshi deportes", "M8/ofi" — no existen en Polybot). Los
NÚMEROS del endpoint son confiables; su narrativa se descarta (Lección 2).

## Diagnóstico F2 CERRADO (2026-07-23, /stats/edges sobre 585.040 snapshots)

`gross_gt_0 = 0`: ni UNA vez en 21 días la suma ask_YES+ask_NO bajó de 1.00.
Máximo bruto del mes: -0.001 (primeros minutos de captura). Promedio: -0.030.
**Conclusión: el universo sampling (mercados con rewards) no tiene ineficiencia
NI BRUTA — recalibrar MIN_EDGE_PCT no sirve (no hay nada sobre cero).**

**Decisión humana (Noel, 2026-07-23): OPCIÓN A — pivotar universo al long-tail.**
Implementado `MARKET_DISCOVERY_SOURCE=sampling|all_recent`: all_recent pagina
/markets, filtra binarios activos, EXCLUYE los condition_ids del set sampling
y observa los N más recientes. Default sigue `sampling` — el pivote se activa
por env var en Coolify (ITERAR solo por config).

Para activar el experimento: en Coolify setear `MARKET_DISCOVERY_SOURCE=all_recent`
+ redeploy. Correr ≥7 días de shadow en el nuevo universo y re-evaluar F2 con
/stats/daily y /stats/edges. Si los skips `no_books` dominan el funnel, el
long-tail elegido no tiene libros vivos → re-pivotear o pasar a la opción B
(tesis neg-risk multi-outcome, F0 discovery primero). Archivar el motor sigue
siendo resultado válido.

Deploy: commit `f3f92d1`, branch `claude/markdown-guide-goal-9mymiy`,
contenedor `Running (healthy)`, host `104.236.211.240:18081` → `:8080` interno.
Volúmenes propios: `polybot_data:/app/data`, `polybot_logs:/app/logs`,
`polybot_secrets:/app/secrets(:ro)`. Kalshi intacto en `:18080`.

## Gate F0 → F1

- [x] `docker build` ok (CI + Coolify)
- [x] Contenedor healthy sirviendo `/health` 200 externo
- [x] `GET /status`: `env:paper`, `shadow_mode:true`, `ws_connected:true`,
      `markets_watched:20`, `last_error:null`, ciclos avanzando
- [x] `smoke_test.py` y `check_no_go.py` verdes en CI
- [x] `pytest` 156 tests, ruff verde
- [x] Secretos fuera del repo; `.gitignore`/`.dockerignore` correctos
- [~] Healthy ≥ 1h continua: reloj arrancó 05:54 UTC — cierre estimado ~06:54 UTC

## Gate F1 → F2 (reloj de 48h de captura CORRIENDO desde 05:54 UTC)

- [x] WS market channel conectado, 20 mercados, eventos persistiéndose
- [x] Firma EIP-712 validada contra vector fijo + ecrecover (tests)
- [ ] WS estable ≥ 48h con gaps/60s ≈ 0 → revisar `analyst_verdicts` y funnel el 2026-07-05
- [ ] Validar firma contra API real (`derive_api_key` 200) con wallet de prueba — pendiente

## Gate F2 → F3 (shadow corriendo; ≥ 7 días desde 2026-07-03)

- [x] Motor 1 evaluando cada 2s con anti-fantasma y RiskManager dry-run
- [ ] 7 días de shadow continuo con PnL teórico > 0 → evaluable desde 2026-07-10

## Gate F3 → F4 (código en seco; NUNCA encendido por el agente)

- [x] Executor + user channel + reconciliación testeados en seco
- [ ] Secretos de F3 pendientes (correcto en paper): wallet key como archivo en
      `/app/secrets` (chmod 400), CLOB creds como env secrets, approvals USDC = humano
- [ ] Encendido (`TRADING_ENABLED=true` + `POLYMARKET_ENV=production`) = decisión humana
      tras gates F1 y F2 verdes + NO-GO en GO

## Incidencias resueltas (2026-07-03)

1. **Branch en Coolify**: apuntaba a `main` (inexistente) → corregido al default.
2. **`CLOB_API_URL` sin protocolo** en env vars → `UnsupportedProtocol` en loop,
   `ws_connected:false`. Fix humano en el panel + hardening en código (PR #3):
   URLs sin esquema se normalizan, endpoints cruzados fallan en el boot.
3. **Typos de env vars** (`AX_SIMULTANEOUS_EXPOSURE_PCT`, `MIN_LIQUIDITY_CONTRACT`)
   → renombrados; mientras estuvieron mal, pydantic usó los defaults hardcoded seguros.

## Incidente 2026-07-11 (detectado 2026-07-23 por el agente web)

`data_capture: OperationalError: database or disk is full` en un INSERT de
`market_snapshots` el 2026-07-11 — la lección "nada sin tope" repetida acá:
`orderbook_events` sin retención llenó el disco a los ~8 días de captura.
El error quedó sticky en `/status` 12.7 días enmascarando errores nuevos.
Además: el contenedor corre desde 2026-07-10 (uptime 13.6d) → los merges del
2026-07-23 (WAL #5, skill #6) NO están deployados; verificar el auto-deploy
de Coolify o redeployar a mano.

Fix (mismo PR de la skill): servicio `maintenance` con retención por tabla
(events 14d / snapshots 30d / funnel 90d), guard de disco de lazo cerrado
(warn 5GB, crítico 2GB → poda agresiva + RiskEvent; la captura nunca se gatea),
`wal_checkpoint(TRUNCATE)` tras podar, `last_error` con TTL 6h y
`disk_free_gb`/`disk_low` en `/status`.

Pendiente humano tras el merge: redeploy + verificar `df -h` del droplet y que
`market_snapshots` recibió INSERTs post-07-11 (¿la captura entre 07-11 y hoy
quedó coja?). Ojo: el reloj de 48h del gate F1 debe evaluarse sobre datos
CONTINUOS — si hubo agujero por disco lleno, el reloj re-arranca post-fix.

## Última iteración

2026-07-03 — Deploy verificado en vivo, bug de URL diagnosticado y corregido
(env var + hardening con 7 tests de regresión), captura estable con 20 mercados.
PRs #1 y #3 mergeados.

## Siguiente paso

1. Confirmar cierre del gate F0 (~06:54 UTC: 1h healthy sin reinicios).
2. Dejar correr la captura; el 2026-07-05 evaluar gate F1 con datos reales:
   `analyst_verdicts` (ws_uptime, gaps) + conteo de `orderbook_events`.
3. En paralelo, cuando haya wallet de prueba: validar `derive_api_key` contra el
   API real (cierra el punto pendiente de F1).
4. Gate F2 evaluable desde 2026-07-10 con el funnel de 7 días.

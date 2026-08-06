# Guía de despliegue — Motores 2 (consenso), 3 (neg-risk), 4 (OFI) y 5 (spillover) — para el agente web

> **Cómo usar esto:** pegá este documento completo como PRIMER mensaje de la
> sesión del agente del navegador, DESPUÉS de pegarle su contexto base
> (`web_agent_context.md`). Esta guía es de UNA campaña concreta: verificar el
> deploy de los motores nuevos y cerrar el veredicto del experimento long-tail. Cuando la
> campaña termine, esta guía se archiva.

**TODO por API.** Cada verificación de esta guía es un `GET` a
`http://104.236.211.240:18081/...` — nada de deducir el estado desde la UI de
Coolify ni desde los logs si un endpoint ya responde la pregunta. Los logs se
miran SOLO donde la guía lo dice explícitamente (una línea de arranque que no
tiene endpoint). *Una estimación no es una medición.*

## Reglas duras de esta campaña (además de las del contexto base)

1. **Solo GET a `:18081`.** El bot expone `POST /admin/pause` y `/admin/resume`:
   JAMÁS los llames, ni "para probar".
2. **Vos NO activás nada.** Las env vars (`MARKET_DISCOVERY_SOURCE`,
   `MOTOR_2_CONSENSUS_ENABLED`, `MOTOR_3_NEG_RISK_ENABLED`, `ODDS_API_KEY`)
   las toca SOLO el humano en Coolify. Tu trabajo
   es verificar y reportar; si un paso requiere cambiar config, tu output es
   "listo para que el humano haga X", nunca hacerlo vos.
3. **`:18081` es POLYBOT (paper, $0). `:18080` es el bot KALSHI (dinero real).**
   Si en tu análisis aparecen `ticker`, `sid`, centavos 0–100, "M8" u "ofi",
   estás mezclando bots — pará y re-verificá. Acá es `condition_id`, USDC
   0.00–1.00, y los motores se llaman `motor_1_arbitrage`, `motor_2_consensus`,
   `motor_3_neg_risk`, `motor_4_ofi` y `motor_5_spillover`. OJO: los DOS bots tienen un "motor 2 de consenso" con
   la misma tesis y la misma API — el de Kalshi opera dinero real, el de acá
   es shadow. El puerto decide de cuál estás hablando.
4. Números literales. Lo que no viste: "no accesible", nunca estimado.

---

## FASE A — Verificación post-merge (el deploy que NO debe cambiar nada)

Contexto: el PR #12 mergea el Motor 2 **apagado por default**. Tras el redeploy
de Coolify, el bot tiene que comportarse EXACTAMENTE igual que antes. Esta fase
existe para probar ese "igual" con datos, no asumirlo.

| # | GET | Esperado | Si NO |
|---|---|---|---|
| A1 | `/health` | 200, `status: ok` | reportar y FRENAR: el deploy falló |
| A2 | `/status` | `env: paper`, `trading_enabled: false`, `shadow_mode: true`, `ws_connected: true`, `last_error: null`. En `motors` NO aparece `motor_3_neg_risk` (está apagado) | si aparece motor_2 → una env var quedó seteada de antes: reportar como ANOMALÍA |
| A3 | `/stats/daily?days=3` | responde normal; los días previos conservan sus conteos (`market_snapshots`, `funnel_cycles`). Claves `m2_*` AUSENTES o en cero | conteos históricos distintos → la migración tocó datos: ANOMALÍA GRAVE, frenar |
| A4 | `/stats/multi` | 200 con `by_direction: {}` y `top_10_recorded: []` (la tabla existe, vacía) | 500 → la migración no corrió: reportar con el body del error |
| A4b | `/stats/consensus` | 200 con `by_status: {}` y `odds_api_quota_breaker_active: false` | 500 → reportar body |
| A5 | `/stats/edges?days=3` | responde en < ~5s (los WHERE ahora usan índice) | timeout/lento → reportar la latencia medida |

**Cierre de FASE A:** las 5 en esperado → reportar "deploy neutro verificado,
listo para FASE B/C cuando el humano decida". Cualquier otra cosa → reporte con
el dato literal y NO seguir a las fases siguientes.

---

## FASE B — Cierre del veredicto long-tail (ANTES de decidir el universo)

Contexto: el experimento `all_recent` (pivote 2026-07-23) tenía veredicto
estimado ~2026-07-30. `neg_risk` y `all_recent` comparten
`MARKET_DISCOVERY_SOURCE`, así que son **mutuamente excluyentes**: el humano
necesita este veredicto para decidir cuál corre. Si el experimento nunca se
activó (el `/status` de FASE A muestra el universo sampling de siempre),
reportá eso y esta fase queda en "sin datos — experimento no corrido".

| # | GET | Qué mirar |
|---|---|---|
| B1 | `/stats/daily?days=7` | continuidad (¿7 días presentes?), `funnel_cycles` > 0, y sobre todo `edges_recorded` y `theoretical_pnl_usd` por día |
| B2 | `/stats/edges?days=7` | `counts_above` — la pregunta del gate: ¿`gross_gt_0` sigue en 0 también en el long-tail? |
| B3 | `/status` | `markets_watched` (¿el long-tail dio mercados?) |

**Lectura del resultado (llevala literal al reporte, la decisión es del humano):**
- `gross_gt_0 = 0` también acá → el long-tail tampoco tiene ineficiencia bruta:
  el veredicto del experimento es negativo y libera el discovery para `neg_risk`.
- `gross_gt_0 > 0` con `edges_recorded = 0` → hay edge bruto que los
  costos/umbral filtran: recalibración por config, decisión humana.
- `edges_recorded > 0` → primer edge shadow real del proyecto: reportar los
  números exactos y el top_10.
- Si el funnel está dominado por `no_books` → el long-tail no tiene libros
  vivos (el supuesto del pivote era falso): también libera para `neg_risk`.

---

## FASE C — Activación del Motor 3 (neg-risk; el humano setea, vos verificás)

Prerrequisito: FASE A verde + decisión del humano sobre FASE B. El humano setea
en Coolify `MARKET_DISCOVERY_SOURCE=neg_risk` y `MOTOR_3_NEG_RISK_ENABLED=true`
y redeploya. Después de eso, tu checklist:

**C0 (T+2 min) — logs del contenedor** (única mirada a logs de la guía):
buscar la línea `Motor 3 (neg-risk multi-outcome) en SHADOW` y que NO haya
tracebacks nuevos. Anotar también la línea `Discovery neg_risk: X grupos
vistos, Y descartados -> observando Z grupos / W patas` — esos 4 números van
al reporte: son el baseline del universo.

| # | GET | Esperado | Señal de alarma |
|---|---|---|---|
| C1 | `/status` | `motors` incluye `motor_3_neg_risk`; `markets_watched` > 0 | `markets_watched: 0` → el universo neg-risk no dio grupos completos (puede ser un resultado real — reportarlo, no maquillarlo) |
| C2 | `/stats/daily?days=1` | `m3_funnel_cycles` acumulando (tick 5s ≈ 17k/día máx) | ausente tras 30 min → el motor no está corriendo |
| C3 | `/stats/multi` | `by_direction` con conteos; mirar `edge_too_high_fantasma` vs `windows_total` | **fantasmas > 20% del total → el guard de grupos incompletos está fallando y NINGÚN número del motor es confiable** — reportar como bloqueante del gate |
| C4 | `/stats/multi` | si hay `shadow_recorded` > 0: top_10 con `net_edge_pct` y `legs` | `net_edge_pct` cerca del tope de 10% en varios → sospechar libros stale aunque el anti-fantasma no haya saltado |
| C5 (T+24h) | `/stats/daily?days=1` + `/stats/multi?days=1` | día completo sin agujeros; distribución del edge | `m3_funnel_cycles` muy por debajo de ~15k → el motor se cae y re-arranca: mirar logs y reportar |

**Rollback (lo ejecuta el humano, vos solo lo sugerís si aplica):** cualquier
excepción nueva en loop, o C3 en alarma → `MOTOR_3_NEG_RISK_ENABLED=false` +
redeploy. Primero se apaga, después se investiga (Lección 9 de Kalshi).

**El gate F2 de M2 NO se evalúa en esta campaña**: son ≥7 días de shadow con
los criterios ya escritos en `docs/handoff/LATEST.md`. Esta guía solo deja el
motor corriendo y medido.

---

## FASE D — Activación del Motor 2 (consenso, The Odds API — la API PAGA)

Prerrequisito: FASE A verde. Independiente de B/C: el M2 consenso NO toca el
discovery (usa los mercados ya observados), así que puede activarse con
cualquier universo. El humano setea en Coolify `ODDS_API_KEY=<secret>` +
`MOTOR_2_CONSENSUS_ENABLED=true` y redeploya. Después:

**D0 (T+2 min) — logs**: buscar `Motor 2 (consenso sportsbooks) en SHADOW` y
cero tracebacks. La línea incluye el recordatorio de la tesis (-$432 en
Kalshi): es intencional, no un error.

| # | GET | Esperado | Señal de alarma |
|---|---|---|---|
| D1 | `/status` | `motors` incluye `motor_2_consensus` | ausente → el flag no tomó |
| D2 | `/stats/consensus` | `odds_api_quota_remaining` con un número (la key funciona) | `null` tras 10 min → la key no está llegando o no hubo request; `odds_api_quota_breaker_active: true` → CUOTA AGOTADA, reportar YA (es plata) |
| D3 | `/stats/daily?days=1` | `m2_funnel_cycles` acumulando (poll 300s ≈ 288/día) | ausente tras 30 min → el motor no corre |
| D4 | `/stats/consensus` | mirar `by_status`: si `no_match` domina, el universo observado no tiene mercados deportivos que matcheen — es un DATO (reportarlo), no un bug | `ambiguous_match` alto → doubleheaders o nombres contenidos: anotar ejemplos |
| D5 | `/stats/consensus` | si hay `shadow_recorded`: top-10 con `net_edge_pp`, `fair_prob`, `market_ask`, `books` | `edge_too_high` > 20% de las señales → matches equivocados o cuotas stale: BLOQUEANTE del gate |
| D6 (T+24h) | `/stats/consensus?days=1` | consumo de cuota del día: anotar `odds_api_quota_remaining` al inicio y al final — la proyección mensual va al reporte | proyección > 80% del plan → reportar para ajustar `MOTOR_2_POLL_SECONDS`/`ODDS_API_CACHE_TTL_SEC` |

**Verificación de matches (la falla silenciosa catastrófica):** tomar hasta 10
filas del top de `/stats/consensus` y verificar A MANO (en polymarket.com,
solo lectura) que la pregunta del mercado corresponde al partido
`home_team vs away_team` de la señal. UN match equivocado = reportar como
bloqueante; el motor queda en cuarentena hasta que Claude Code arregle el
matcher. Esta verificación es parte del gate F2, no opcional.

**Rollback (humano):** `MOTOR_2_CONSENSUS_ENABLED=false` + redeploy. La cuota
gastada no vuelve: si el breaker está activo, apagar el motor igual (no
consume, pero tampoco mide).

---

## FASE E — Activación de los Motores 4 (OFI) y 5 (spillover)

Prerrequisito: FASE A verde. M4 es independiente del universo
(`MOTOR_4_OFI_ENABLED=true`); M5 requiere el discovery en `neg_risk`
(`MOTOR_5_SPILLOVER_ENABLED=true`, típicamente junto a la FASE C).

**E0 (T+2 min) — logs**: para M4 buscar `Motor 4 (OFI) en SHADOW embebido`;
para M5, `Motor 5 (spillover neg-risk) en SHADOW`. OJO: **M4 NO aparece en
`motors` de `/status`** — va embebido en el data capture; su señal de vida es
el endpoint, no la lista.

| # | GET | Esperado | Señal de alarma |
|---|---|---|---|
| E1 | `/stats/ofi` | 200; `signals_measured` va a tardar en moverse (baseline de 200 muestras por token + z>=3) — 0 el primer día puede ser normal | 0 señales tras 48h con WS activo → baseline nunca madura o el z_min es alto para este venue: reportar para diagnóstico |
| E2 | `/stats/ofi?days=1` | mirar `move60_pp_median` — la PREGUNTA del gate. Referencia Kalshi: p50 +3.18pp | \|mediana\| enorme (>10pp) con pocas señales → books finos moviendo el mid: anotar, no celebrar |
| E3 | `/stats/spillover` | 200; `windows_measured` crece solo si hay grupos Y saltos >=5pp | 0 ventanas con grupos activos y mercados moviéndose → el umbral de trigger puede ser alto: es un DATO del venue, reportarlo |
| E4 (T+24h) | ambos | conteos del día + medianas | cualquier 500 → reportar body |

Recordatorio de unidades para el reporte: `zscore` es ADIMENSIONAL (no es un
%), los `_pp` son puntos de probabilidad. La ausencia de |z| < 3 en los datos
del M4 es el umbral del detector, NO un agujero de captura — no la reportes
como anomalía (ese falso artefacto ya nos pasó en Kalshi el 07-28).

---

## Formato del reporte (tu output SIEMPRE termina así)

```
## REPORTE POLYBOT — [fecha/hora UTC]
Campaña: despliegue Motores 2/3/4/5 — FASE [A|B|C|D|E]
Fuente(s): [URLs exactas consultadas]
Checklist:
- [A1..E4 que corriste]: [esperado ✓ / dato literal si no]
Hallazgos:
- [números exactos, tal cual el JSON]
Anomalías/sospechas: [o "ninguna"]
Listo para que el humano: [siguiente acción concreta, o "nada pendiente"]
Acción sugerida para Claude Code: [1 línea, o "ninguna"]
```

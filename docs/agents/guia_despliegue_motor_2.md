# Guía de despliegue — Motor 2 (neg-risk) — para el agente web

> **Cómo usar esto:** pegá este documento completo como PRIMER mensaje de la
> sesión del agente del navegador, DESPUÉS de pegarle su contexto base
> (`web_agent_context.md`). Esta guía es de UNA campaña concreta: verificar el
> deploy del Motor 2 y cerrar el veredicto del experimento long-tail. Cuando la
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
   `MOTOR_2_NEG_RISK_ENABLED`) las toca SOLO el humano en Coolify. Tu trabajo
   es verificar y reportar; si un paso requiere cambiar config, tu output es
   "listo para que el humano haga X", nunca hacerlo vos.
3. **`:18081` es POLYBOT (paper, $0). `:18080` es el bot KALSHI (dinero real).**
   Si en tu análisis aparecen `ticker`, `sid`, centavos 0–100, "M8" u "ofi",
   estás mezclando bots — pará y re-verificá. Acá es `condition_id`, USDC
   0.00–1.00, y los motores se llaman `motor_1_arbitrage` y `motor_2_neg_risk`.
4. Números literales. Lo que no viste: "no accesible", nunca estimado.

---

## FASE A — Verificación post-merge (el deploy que NO debe cambiar nada)

Contexto: el PR #12 mergea el Motor 2 **apagado por default**. Tras el redeploy
de Coolify, el bot tiene que comportarse EXACTAMENTE igual que antes. Esta fase
existe para probar ese "igual" con datos, no asumirlo.

| # | GET | Esperado | Si NO |
|---|---|---|---|
| A1 | `/health` | 200, `status: ok` | reportar y FRENAR: el deploy falló |
| A2 | `/status` | `env: paper`, `trading_enabled: false`, `shadow_mode: true`, `ws_connected: true`, `last_error: null`. En `motors` NO aparece `motor_2_neg_risk` (está apagado) | si aparece motor_2 → una env var quedó seteada de antes: reportar como ANOMALÍA |
| A3 | `/stats/daily?days=3` | responde normal; los días previos conservan sus conteos (`market_snapshots`, `funnel_cycles`). Claves `m2_*` AUSENTES o en cero | conteos históricos distintos → la migración tocó datos: ANOMALÍA GRAVE, frenar |
| A4 | `/stats/multi` | 200 con `by_direction: {}` y `top_10_recorded: []` (la tabla existe, vacía) | 500 → la migración no corrió: reportar con el body del error |
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

## FASE C — Activación del Motor 2 (el humano setea, vos verificás)

Prerrequisito: FASE A verde + decisión del humano sobre FASE B. El humano setea
en Coolify `MARKET_DISCOVERY_SOURCE=neg_risk` y `MOTOR_2_NEG_RISK_ENABLED=true`
y redeploya. Después de eso, tu checklist:

**C0 (T+2 min) — logs del contenedor** (única mirada a logs de la guía):
buscar la línea `Motor 2 (neg-risk multi-outcome) en SHADOW` y que NO haya
tracebacks nuevos. Anotar también la línea `Discovery neg_risk: X grupos
vistos, Y descartados -> observando Z grupos / W patas` — esos 4 números van
al reporte: son el baseline del universo.

| # | GET | Esperado | Señal de alarma |
|---|---|---|---|
| C1 | `/status` | `motors` incluye `motor_2_neg_risk`; `markets_watched` > 0 | `markets_watched: 0` → el universo neg-risk no dio grupos completos (puede ser un resultado real — reportarlo, no maquillarlo) |
| C2 | `/stats/daily?days=1` | `m2_funnel_cycles` acumulando (tick 5s ≈ 17k/día máx) | ausente tras 30 min → el motor no está corriendo |
| C3 | `/stats/multi` | `by_direction` con conteos; mirar `edge_too_high_fantasma` vs `windows_total` | **fantasmas > 20% del total → el guard de grupos incompletos está fallando y NINGÚN número del motor es confiable** — reportar como bloqueante del gate |
| C4 | `/stats/multi` | si hay `shadow_recorded` > 0: top_10 con `net_edge_pct` y `legs` | `net_edge_pct` cerca del tope de 10% en varios → sospechar libros stale aunque el anti-fantasma no haya saltado |
| C5 (T+24h) | `/stats/daily?days=1` + `/stats/multi?days=1` | día completo sin agujeros; distribución del edge | `m2_funnel_cycles` muy por debajo de ~15k → el motor se cae y re-arranca: mirar logs y reportar |

**Rollback (lo ejecuta el humano, vos solo lo sugerís si aplica):** cualquier
excepción nueva en loop, o C3 en alarma → `MOTOR_2_NEG_RISK_ENABLED=false` +
redeploy. Primero se apaga, después se investiga (Lección 9 de Kalshi).

**El gate F2 de M2 NO se evalúa en esta campaña**: son ≥7 días de shadow con
los criterios ya escritos en `docs/handoff/LATEST.md`. Esta guía solo deja el
motor corriendo y medido.

---

## Formato del reporte (tu output SIEMPRE termina así)

```
## REPORTE POLYBOT — [fecha/hora UTC]
Campaña: despliegue Motor 2 — FASE [A|B|C]
Fuente(s): [URLs exactas consultadas]
Checklist:
- [A1..C5 que corriste]: [esperado ✓ / dato literal si no]
Hallazgos:
- [números exactos, tal cual el JSON]
Anomalías/sospechas: [o "ninguna"]
Listo para que el humano: [siguiente acción concreta, o "nada pendiente"]
Acción sugerida para Claude Code: [1 línea, o "ninguna"]
```

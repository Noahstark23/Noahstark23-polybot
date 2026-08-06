# Plan de motores de Polybot — derivado de la auditoría del bot Kalshi

> Regla del plan: **cada motor se construye o se descarta por el VEREDICTO que
> su tesis obtuvo en Kalshi con datos reales** (auditoría 2026-07-18 + mes de
> prueba 07-28), no por entusiasmo. Todo motor nuevo entra por F2 en SHADOW,
> apagado por default, con gate a priori — regla de ARCHITECTURE §6: ningún
> motor se salta fases. El Obsidian/lecciones destiladas viven en
> `docs/lecciones_kalshi.md`; este plan es su aplicación motor por motor.

## Mapa: tesis de Kalshi → decisión en Polybot

| Kalshi | Veredicto allá (datos) | Polybot | Decisión |
|---|---|---|---|
| M1 arb intra-venue | +$33.37/519 trades — media positiva, significancia pendiente (t-stat nuevo) | **Motor 1** | ✅ construido (F2, universo en diagnóstico) |
| M2 consenso sportsbooks | **−$432 reales**; edge techo 0.15pp vs umbral 3pp | **Motor 2** | ✅ construido como RE-TEST barato en otro venue (decisión del owner: la API paga se aprovecha). Advertencia a priori en código/log/handoff |
| REST multi-outcome | edge DETECTADO real (3.13pp); ejecución rota (73% rollback) | **Motor 3** (neg-risk) | ✅ construido — la detección valió, la ejecución queda para F3 con hard-leg-first |
| M8 OFI | **"la única promesa viva"**: p50 +3.18pp a T+60 (n=130) | **Motor 4** | ✅ SE CONSTRUYE (esta iteración) |
| M9 spillover | shadow prometedor; instrumento sano una vez separadas las unidades | **Motor 5** | ✅ SE CONSTRUYE (esta iteración) — sinergia directa: reusa los grupos neg-risk del M3 |
| M6 line-move | **MUDO**: 0 señales en un mes de captura | — | ❌ NO se porta. "Archivar un motor que no rinde es un resultado válido y barato". Si algún día se re-abre, primero se explica por qué acá calló |
| M5 market maker | shadow con fills, sin veredicto; requiere quotear (órdenes reales) | — | ⏸ DIFERIDO a F3+: un MM no tiene versión shadow honesta sin resting orders. Se reevalúa cuando Polybot ejecute |
| M3 CLV / salidas | estable (gestor de posiciones) | — | ⏸ DIFERIDO a F3: no hay posiciones que gestionar hasta que haya órdenes reales |

## Por qué M4 y M5 son los siguientes (y no otros)

1. **Son los únicos con veredicto POSITIVO o prometedor en Kalshi** que se
   pueden medir en shadow puro (sin postear una orden).
2. **Costo marginal mínimo**: M4 se alimenta del stream WS que ya corre; M5
   reusa los grupos neg-risk del discovery del M3. Cero APIs nuevas, cero
   costo de cuota.
3. **Riesgo cero**: ambos son instrumentos de medición. El peor resultado
   posible es "la tesis no replica en Polymarket", que es un dato barato.

## Lecciones aplicadas por diseño en M4/M5 (no negociables)

- **Una columna, una unidad** (incidente edge_pct polimórfica, 07-28): el
  z-score del OFI va en SU columna `zscore`; los moves van en `_pp` (puntos de
  probabilidad). Nada se llama `edge_pct` sin ser un porcentaje.
- **El hueco del detector no es un artefacto**: un detector con umbral no
  emite señal entre 0 y z_min — documentado en el modelo para que nadie vuelva
  a leer esa ausencia como data podrida.
- **Lección 7 (la captura no se rompe)**: el hook del OFI en el data capture
  es best-effort total — cualquier excepción se loguea y el stream sigue.
- **Nada sin tope**: baseline con `maxlen`, cooldown anti-ráfaga por token,
  pendientes con gracia y descarte, retención de las tablas nuevas en el
  mismo commit, y de-dupe donde aplique.
- **El shadow no asume dirección**: igual que el M8 de Kalshi, se registra la
  presión y el movimiento REAL posterior; si conviene ir a favor (momentum) o
  en contra (contrarian) lo deciden los datos del gate, no el diseño.
- **Gates a priori**: escritos en el handoff ANTES de encender (abajo).

## Lo que NO está en este plan, explícitamente

- Ejecución de ningún motor nuevo (no existe executor de M2/M3/M4/M5 en el
  repo). El único executor es el del M1, gateado por TRADING_ENABLED + NO-GO.
- M6 line-move (mudo en Kalshi — no se re-aprende lo ya medido).
- Market making (sin versión shadow honesta).
- Cross-venue Kalshi↔Polymarket: sigue siendo F4 con su propio doc de fase 0.

# Catálogo de motores del bot Kalshi (referencia para NO mezclar contexto)

Generado del estudio del repo `Noahstark23/botkalshi` (2026-07-23). Propósito:
que cualquier agente de Polybot pueda verificar si un término/motor pertenece
al bot Kalshi (`:18080`, dinero real) y no a Polybot (`:18081`, un solo motor).

**Regla de uso:** si un análisis de Polybot menciona algo de este catálogo,
se mezcló contexto → descartar y re-verificar contra `/status` de `:18081`.

## Motores del bot Kalshi (con veredicto de su auditoría 2026-07-18)

| Motor | Tesis | Estado/veredicto | Flags principales |
|---|---|---|---|
| **M1** Arbitraje intra-Kalshi | YES+NO del mismo ticker < $1 (WS) | ⚫ No es alpha (+$0.065/trade = ruido; 0 ventanas binarias históricas) | `MOTOR_1_ARBITRAGE_ENABLED`, `MOTOR_1_EXECUTION_ENABLED` |
| **M2** Consenso sportsbook | Kalshi vs fair no-vig (The Odds API), >3pp | 🔴 SIN edge (−$432.95, 100% de la sangría; edge techa 0.15pp) — apagar entradas | `MOTOR_2_SPORTSBOOK_ENABLED`, `MOTOR_2_ENTRY_EXECUTION_ENABLED`, `MOTOR_2_EXECUTION_ENABLED` (¡tres flags distintos!) |
| **M3** CLV / Salidas | NO abre; gestiona cierres (take-profit, trailing, T-30) | ✅ Estable | `MOTOR_3_CLV_ENABLED` |
| **M4** Cross-venue Kalshi↔Polymarket | Arb entre venues | Solo diseño (`docs/motor_4_cross_venue_fase0.md`) — SIN código | — |
| **M5** Market Maker | Quotes GTC post_only alrededor del fair | Shadow F1 | `MOTOR_MM_ENABLED`, `MOTOR_MM_EXECUTION_ENABLED` (+ llave `MOTOR_MM_F3_ACK`) |
| **M6** Line-move follower | Sigue saltos del consenso que Kalshi no digirió | ⚫ Mudo (0 señales/mes) — candidato a archivar | `MOTOR_6_LINEMOVE_ENABLED` |
| **M8** OFI (Order Flow Imbalance) | Z-score de desequilibrio del flujo de órdenes | 🟡 Única promesa viva (p50 +3.18pp, n=130 — falta muestra) | `MOTOR_8_OFI_ENABLED` |
| **M9** Spillover | Salto en un market debe derramar a hermanos del evento | Shadow F1 | `MOTOR_9_SPILLOVER_ENABLED` |
| **REST** Arb multi-outcome | Winner-take-all ≥3 patas < $1 | ⚫ Inejecutable (73% rollback, FOK no atómico) — apagar ejecución | `MOTOR_REST_ENABLED`, `MOTOR_REST_EXECUTION_ENABLED` |

Servicios de soporte de su runner: health, data capture (host del
OrderbookManagerV2 + pasajeros M8/M9), settlement poller (settlea TODOS los
motores, corre siempre), balance refresh, memory monitor, DB maintenance,
disk guard, telegram dashboard/command center, analyst loop, daily PnL,
watchdog.

## Vocabulario EXCLUSIVO del bot Kalshi (si aparece en Polybot = contaminación)

- **Motores numerados** (M1…M9, Motor REST) — Polybot tiene UN motor: `motor_1_arbitrage` (mismo nombre que el M1 de Kalshi pero distinto venue y código).
- **`ticker` / `market_ticker`** (`KXMLB-26-ATL`) y **`sid`** — identidad de mercado de Kalshi. Polybot usa `condition_id` + `token_id`.
- **`OrderbookManagerV2`**, **`SidGapError`**, **`FairValueBook`** — infra Kalshi.
- **`EdgeWindow.kind`** (`binary|multi_outcome|consensus|linemove|ofi|spillover`) — el EdgeWindow de Polybot NO tiene campo `kind`.
- **`Motor2FunnelSnapshot`** / `max_tickers` / `MOTOR_MM_MAX_TICKERS` — Kalshi.
- **Precios en cents enteros 0–100** y fee `ceil(7·count·price·(100−price)/10000)` — Polybot usa 0.00–1.00 USDC y fee bps × min(p, 1−p).
- **Auth RSA-PSS** (`KALSHI-ACCESS-*`), The Odds API, consenso sportsbook — Polybot usa EIP-712/EVM y no consume odds externas.

## Contraste rápido: qué ES Polybot

`/status` de `:18081` lista exactamente: `data_capture, motor_1_arbitrage,
analyst, maintenance`. Eso es TODO. Motor 1 de Polybot = arbitraje
intra-mercado Polymarket (`1 − ask_YES − ask_NO − costos`), en shadow, con
universo configurable (`MARKET_DISCOVERY_SOURCE`). Nada más existe.

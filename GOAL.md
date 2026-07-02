# GOAL.md — Objetivo del proyecto Polybot

## Objetivo

Construir un bot algorítmico para **Polymarket** (arbitraje intra-mercado como Motor 1),
desplegado en el **mismo droplet** que el bot Kalshi pero **totalmente aislado** de él
(contenedor, DB, volúmenes, puerto `:18081` y secrets propios). Reutiliza ~70% del
andamiaje de `Noahstark23/botkalshi` y reemplaza sólo la capa del venue
(auth EIP-712/EVM, cliente CLOB REST/WS, modelo de mercado).

**La guía completa es `ARCHITECTURE.md`. El manual de auto-ejecución es `PROJECT_LOOP.md`.
El estado vivo del proyecto está en `docs/handoff/LATEST.md`.**

## Resultado esperado al final del sprint

Infraestructura completa y aislada, corriendo en shadow (captura de datos + detección de
edges sin ejecutar), con el trading real a un solo cambio de config de distancia — pero
sólo cuando los ≥7 días de shadow y los gates lo permitan. **Nada de dinero real forzado
dentro del sprint.**

## Calendario del sprint de 7 días (las fechas son estimaciones; los gates son la autoridad)

| Día | Fase | Trabajo | Meta del día |
|---|---|---|---|
| 1 | F0 Scaffold | Portar infra (runner, config, risk, storage, health, Dockerfile), stubs de auth/clients, `smoke_test.py` | `docker build` verde y contenedor healthy en Coolify respondiendo en `104.236.211.240:18081/health` |
| 2 | F1-A Auth | `eip712_signer.py` con eth-account, firma validada contra vector fijo, derivar credenciales L2 del CLOB | Firma verificable + `get_balance_allowance()` funcionando contra wallet de prueba |
| 3 | F1-B Data Capture | `polymarket_ws.py` (market channel + reconexión), `OrderbookManager` por `token_id`, `data_capture.py` persistiendo snapshots | Captura corriendo — empieza el reloj de las ≥48h del gate F1 |
| 4 | F2-A Detección shadow | `motor_1_arbitrage/engine.py` con filtro anti-fantasma desde el día 1, `fees.py` reescrito, registro en `EdgeWindow`, RiskManager en dry-run | Empieza el shadow — arranca el reloj de los ≥7 días del gate F2 |
| 5 | F2-B Instrumentación | `analyst_loop` + `PolyFunnelSnapshot` + digest de Telegram; tests (pytest) de motor y risk | El funnel diario produce un `AnalystVerdict` legible cada día |
| 6 | F3 preparada, NO encendida | `executor.py` (postea órdenes firmadas), user channel del WS, reconciliación on-chain↔DB, `check_no_go.py` | Código de ejecución completo y testeado en seco, con `TRADING_ENABLED=false` |
| 7 | Cierre y handoff | Consolidar `ARCHITECTURE.md`, `docs/handoff/` con el estado de cada gate, verificar captura y shadow sanos | Polybot desplegado, capturando y detectando en shadow, esperando gates para que el humano encienda trading con capital mínimo |

## Límites duros (ver ARCHITECTURE.md §9 y PROJECT_LOOP.md)

- `TRADING_ENABLED=true` y `POLYMARKET_ENV=production` los pone **el humano**, nunca el agente.
- Approvals de USDC / allowances / cualquier tx on-chain de custodia: **el humano**, jamás el bot.
- Paper/shadow ≥ 7 días antes de dinero real. Nunca avanzar de fase con gate rojo.
- Cero LLMs en el hot path de trading. Cero secrets en el repo.

## Pendiente de seguridad (fuera de este repo)

El repo base `botkalshi` tiene `config/kalshi_private_key.pem` commiteado. Rotar esa API
key y purgar el `.pem` del historial de git (ver ARCHITECTURE.md §10).

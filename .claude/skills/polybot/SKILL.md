---
name: polybot
description: Contexto operativo completo de Polybot (bot Polymarket) para cualquier agente que trabaje en este proyecto — estado vivo, reglas duras, arquitectura, comandos y protocolo de comunicación entre agentes. Usar al inicio de CUALQUIER tarea sobre Polybot.
---

# Skill Polybot — contexto y reglas para agentes

Sos un agente trabajando en **Polybot**, bot algorítmico para Polymarket de Noel
Pineda. Este skill te da el contexto que no tenés por arrancar sin memoria.

## Qué es el proyecto (30 segundos)

Bot de arbitraje intra-mercado (`edge = 1 − ask_YES − ask_NO − costos`) para
Polymarket. Python 3.12 + asyncio + SQLite (SQLModel) + FastAPI. Corre en Docker
(Coolify) en el droplet `104.236.211.240`, contenedor AISLADO del bot Kalshi
que opera dinero real en la misma máquina. Salud: `http://104.236.211.240:18081/status`.

Estado actual: **paper + shadow** — captura orderbooks por WS, detecta edges y
los registra con PnL teórico. **NO postea órdenes.** El código de ejecución
(F3) existe pero está apagado detrás de `TRADING_ENABLED=false`.

## Reglas NO negociables (violarlas = parar y avisar al humano)

1. NUNCA poner `TRADING_ENABLED=true` ni `POLYMARKET_ENV=production` — humano.
2. NUNCA ejecutar approvals de USDC / allowances / tx on-chain — humano, una vez.
3. NUNCA operar, depositar, ni clickear Buy/Sell en polymarket.com — el bot
   firma órdenes del CLOB por API cuando el humano lo habilite; un agente JAMÁS.
4. NUNCA modificar límites de riesgo hardcoded (stop-loss -3/-8/-15%,
   exposición 25%, sizing 5%, ¼ Kelly).
5. NUNCA commitear secretos (.env, .pem, keys). Wallet key sólo en secret volume.
6. NUNCA avanzar de fase con gate rojo. Los gates mandan, las fechas no.
7. Cero LLMs en el hot path de trading.
8. Merge a la rama default = deploy automático en Coolify. Ningún merge de
   runtime sin OK del humano.

## Dos bots en el droplet — diferenciarlos SIEMPRE

En `104.236.211.240` conviven Polybot (`:18081`, paper/shadow, UN motor:
`motor_1_arbitrage`) y el bot Kalshi (`:18080`, DINERO REAL, motores numerados
M1/M2/M5/M8/M9, vocabulario propio: tickers, sids, OrderbookManagerV2,
FairValueBook). Si un análisis de Polybot menciona motores numerados o ese
vocabulario, se mezcló contexto: descartarlo y re-verificar. Catálogo de
referencia del bot Kalshi: `docs/agents/motores_kalshi.md`.

## Documentos fuente (leer según la tarea)

- `ARCHITECTURE.md` — doc maestro: infra, fases F0→F4, gates.
- `PROJECT_LOOP.md` — el ciclo de cada iteración.
- `docs/handoff/LATEST.md` — estado vivo: fase, gates, siguiente paso.
- `docs/lecciones_kalshi.md` — NORMATIVO: lecciones del bot Kalshi y del vault
  del autor (NortexVault). Toda decisión nueva se contrasta acá.

## Mapa del código

```
src/runner.py              orquestador (servicios por fase, supervisor pattern)
src/utils/config.py        settings; límites de riesgo con cota hardcoded
src/risk/manager.py        stop-losses, kill-switch, exposición, reservas
src/auth/eip712_signer.py  firma ClobAuth (L1) + órdenes CTF Exchange
src/clients/               polymarket_clob.py (REST) / polymarket_ws.py (WS)
src/marketdata/            OrderbookManager (libros + gaps) + registry
src/strategies/            data_capture (WS -> SQLite)
src/motor_1_arbitrage/     engine (SHADOW, anti-fantasma) + executor (gated)
src/analytics/             analyst_loop (funnel diario, veredicto puro)
src/reconcile/             posiciones on-chain <-> DB
scripts/                   smoke_test, check_no_go, clear_kill_switch (humano)
```

## Comandos de verificación (correr antes de proponer cualquier cambio)

```bash
pytest -q                      # suite completa (158+ tests)
ruff check src/ tests/ scripts/
python -m scripts.smoke_test   # infra + CLOB read-only
python -m scripts.check_no_go  # checklist NO-GO (§8)
```

## Protocolo de comunicación entre agentes

Los agentes NO comparten memoria. El canal es este repo + el humano:

- **Estado y hallazgos** → actualizar `docs/handoff/LATEST.md` (formato: estado,
  gate verde/rojo, causa si rojo, siguiente paso EXACTO).
- **Lecciones nuevas** → agregar a `docs/lecciones_kalshi.md`.
- **Datos para otro agente** → escribir en `docs/agents/` como markdown con
  fecha, y avisarle al humano qué archivo leer.
- **Agente web (navegador)** → usa `docs/agents/web_agent_context.md` como su
  brief; devuelve hallazgos en el formato de reporte definido ahí, y el humano
  los pega en la sesión de Claude Code (o los sube al repo).

## Fase actual y relojes (ver LATEST.md para el estado fresco)

- F0 (contenedor healthy) — cerrado tras el deploy de 2026-07-03.
- F1 (48h captura, gaps ≈ 0) — evaluable desde 2026-07-05.
  Pendiente extra: validar `derive_api_key` contra el API real con wallet de
  prueba (regla del vault: "validar sensores contra API viva ES el gate").
- F2 (7 días shadow, PnL teórico > 0) — evaluable desde 2026-07-10.
- F3 (encendido real) — decisión humana tras F1+F2 verdes + NO-GO en GO.

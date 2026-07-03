# Polybot

Bot algorítmico para **Polymarket** (arbitraje intra-mercado como Motor 1).
Corre en el mismo droplet que el bot Kalshi pero **totalmente aislado**: contenedor,
DB, volúmenes, secrets y puerto (`:18081`) propios.

- Objetivo y calendario: [`GOAL.md`](GOAL.md)
- Documento maestro (infra, fases F0→F4, gates): [`ARCHITECTURE.md`](ARCHITECTURE.md)
- Loop de auto-ejecución: [`PROJECT_LOOP.md`](PROJECT_LOOP.md)
- Estado vivo: [`docs/handoff/LATEST.md`](docs/handoff/LATEST.md)

## Arranque local

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

python -m scripts.smoke_test    # verifica infra + CLOB read-only
python -m scripts.check_no_go   # checklist NO-GO (§8)
python -m src.runner            # health + data capture + motor 1 en SHADOW
curl http://localhost:8080/health
```

## Con Docker

```bash
docker build -t polybot .
docker run --rm -p 18081:8080 \
  -v "$(pwd)/data:/app/data" -v "$(pwd)/logs:/app/logs" \
  --env-file .env polybot
curl http://localhost:18081/health
```

## Tests y lint

```bash
pytest -q
ruff check src/ tests/ scripts/
```

## Qué hace hoy (y qué NO)

Arranca en **paper + shadow**: captura orderbooks por WS, detecta edges de
arbitraje intra-mercado (`1 − ask_YES − ask_NO − costos`), los registra en
`edge_windows` con PnL teórico y emite un `FunnelSnapshot` por ciclo + un
`AnalystVerdict` diario. **No postea ni una orden.**

El código de ejecución (F3) existe y está testeado en seco, pero:

- `TRADING_ENABLED=true` y `POLYMARKET_ENV=production` los pone **el humano** en Coolify.
- El checklist NO-GO (`scripts/check_no_go.py`) bloquea el arranque con dinero si algo
  está en rojo (shadow < 7 días, kill-switch activo, reconciliación con discrepancia,
  límites diluidos, secretos mal puestos).
- Los approvals de USDC on-chain los hace el humano una única vez; el bot sólo firma
  órdenes del CLOB (EIP-712 off-chain).
- La wallet key vive SOLO en el secret volume (`POLY_SIGNER_KEY_PATH`), jamás en `.env`
  ni en el repo.

## Estructura

```
src/
├── runner.py               orquestador asyncio (servicios por fase)
├── utils/config.py         settings pydantic; límites de riesgo NO diluibles
├── db/models.py            Trade/Position/MarketSnapshot/EdgeWindow/Funnel/...
├── risk/manager.py         stop-loss -3%/-8%/-15%, kill-switch, exposición 25%, sizing 5%
├── auth/eip712_signer.py   ClobAuth (L1) + órdenes CTF Exchange (vector fijo en tests)
├── clients/                polymarket_clob.py (REST) + polymarket_ws.py (market/user)
├── marketdata/             OrderbookManager (libros locales + gaps) + registry
├── strategies/             data_capture (WS -> SQLite)
├── motor_1_arbitrage/      engine (SHADOW, anti-fantasma) + executor (F3, gated)
├── analytics/              analyst_loop (funnel diario, veredicto puro, sin LLM)
├── reconcile/              posiciones on-chain <-> DB (pausa preventiva)
├── monitoring/             digest de Telegram
└── api/health.py           /health /ready /status /admin/pause|resume
```

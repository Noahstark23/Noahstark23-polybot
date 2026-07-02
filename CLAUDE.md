# CLAUDE.md — Polybot

Bot algorítmico para Polymarket. Python 3.12, asyncio, SQLite (SQLModel), FastAPI health
server, deploy Docker en Coolify (droplet compartido con el bot Kalshi, contenedor aislado).

## Lee estos archivos antes de trabajar

1. **`GOAL.md`** — objetivo del proyecto y calendario del sprint.
2. **`ARCHITECTURE.md`** — documento maestro: infraestructura, qué se reutiliza del repo
   base `Noahstark23/botkalshi`, capa del venue Polymarket, fases F0→F4 con sus gates.
3. **`PROJECT_LOOP.md`** — el ciclo de auto-ejecución de cada iteración.
4. **`docs/handoff/LATEST.md`** — estado vivo: fase activa, gate pendiente, siguiente paso.

## Reglas no negociables (resumen — detalle en ARCHITECTURE.md §9 y PROJECT_LOOP.md)

- NUNCA poner `TRADING_ENABLED=true` ni `POLYMARKET_ENV=production` — eso lo hace el humano.
- NUNCA ejecutar approvals de USDC / allowances / tx on-chain de custodia.
- NUNCA modificar límites de riesgo hardcoded (stop-loss, exposición, sizing, Kelly).
- NUNCA commitear llaves, `.env` real, ni ningún secreto. La wallet key va sólo en secret volume.
- NUNCA avanzar de fase con un gate en rojo ("vamos atrasados" no es razón).
- Cero LLMs en el hot path de trading.
- No colisionar con el contenedor Kalshi: puerto host `:18081`, volúmenes y secrets propios.

## Comandos

```bash
pip install -e ".[dev]"        # setup local
pytest -q                      # tests
ruff check src/ tests/         # lint
python -m scripts.smoke_test   # smoke test (F0+)
python -m scripts.check_no_go  # checklist NO-GO
```

## Al cerrar cada iteración

Actualizar `docs/handoff/LATEST.md` con estado, gate (verde/rojo) y siguiente paso.

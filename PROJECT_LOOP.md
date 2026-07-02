# PROJECT_LOOP.md — Instrucciones de auto-ejecución (Fable 5)

Eres el ingeniero autónomo de Polybot. Ejecutas el loop de ingeniería sin
supervisión continua. Lee este archivo al inicio de CADA iteración.

## Estado actual
Fase activa y gate pendiente: ver `docs/handoff/LATEST.md`.

## Ciclo de cada iteración (repetir)
1. LEER  `docs/handoff/LATEST.md` (fase actual, gate, causa de rojo si aplica)
         y los `PolyFunnelSnapshot` acumulados en la DB.
2. PLAN  determinar la tarea más pequeña que acerca al gate de la fase actual.
3. CODE  editar SOLO los archivos que la fase (ARCHITECTURE.md §6) autoriza.
4. TEST  correr `pytest -q`, `ruff check`, `scripts/smoke_test.py`, `scripts/check_no_go.py`.
5. VERIFY evaluar el gate de la fase con datos reales, no supuestos.
6. RECORD escribir el resultado en `docs/handoff/LATEST.md` (estado + gate + siguiente paso).
7. Si el gate está VERDE -> avanzar de fase. Si ROJO -> documentar causa y re-iterar.

## Límites que NUNCA cruzas sin instrucción humana explícita en el chat
- NO poner `TRADING_ENABLED=true`.
- NO poner `POLYMARKET_ENV=production`.
- NO ejecutar approvals de USDC / allowances / cualquier tx on-chain de custodia.
- NO modificar los valores de riesgo hardcoded (stop-loss, exposición, sizing, Kelly).
- NO commitear llaves, .env, o cualquier secreto.
- NO avanzar de fase con un gate en rojo ("vamos atrasados" NO es razón — regla anti-Lección-9).
- NO crear cuentas, repos o dar permisos: eso lo hace el humano.

## Regla de oro
Los gates son la autoridad. Las fechas del calendario son estimaciones.
Instrumentar SIEMPRE antes de actuar. Cero LLMs en el hot path de trading.

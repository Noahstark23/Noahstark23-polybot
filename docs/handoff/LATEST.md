# HANDOFF — Estado del proyecto

Fase activa: **F0 cerrando / F1 en curso** — bot DESPLEGADO en Coolify y
capturando en paper/shadow desde 2026-07-03 05:54 UTC. Cero órdenes reales.

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

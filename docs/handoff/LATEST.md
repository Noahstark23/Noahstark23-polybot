# HANDOFF — Estado del proyecto

Fase activa: **F0 — Scaffold & Discovery** (gate F0→F1 parcialmente verde;
lo que falta requiere el deploy en Coolify, no más código).

Código construido hasta F3 inclusive (los gates NO se saltaron: F1/F2/F3 son
gates de *activación* que corren con el bot desplegado; el código está listo
y testeado en seco, y el trading sigue apagado).

## Gate F0 → F1

- [x] Runner arranca sin errores, health server responde (verificado local: exit 0 con SIGTERM)
- [x] `GET /health` 200; `GET /status` JSON con `started_at` y `ws_connected:false`
- [x] `smoke_test.py` verde local (check CLOB queda SKIPPED aquí: el sandbox no
      tiene salida a polymarket.com; en CI y en el droplet corre completo)
- [x] `pytest` de config y risk/manager al 100% (149 tests totales, ruff verde)
- [x] `.pem`/keystore fuera del repo; `.gitignore` + `.dockerignore` correctos
- [ ] `docker build` ok (sin daemon en el sandbox — lo valida CI/Coolify)
- [ ] Contenedor `healthy` ≥ 1h en Coolify (`104.236.211.240:18081/health`) — **requiere deploy humano** (§0.3)

## Gate F1 → F2 (código listo, reloj corre tras el deploy)

- [x] Firma EIP-712 validada contra vector fijo + ecrecover (tests)
- [x] `derive_api_key`, `get_balance_allowance`, `build_order` (firma, NO postea) implementados
- [ ] Firma validada contra el API real / credenciales L2 usables — requiere red + wallet de prueba
- [ ] WS estable ≥ 48h, gaps/60s ≈ 0 — requiere deploy

## Gate F2 → F3 (código listo, reloj de 7 días corre tras el deploy)

- [x] Motor 1 shadow con filtro anti-fantasma (`edge_too_high`) desde el día 1
- [x] `EdgeWindow` + `FunnelSnapshot` por ciclo + `AnalystVerdict` diario (funciones puras)
- [x] RiskManager dry-run registra decisión sin reservar
- [ ] ≥ 7 días de shadow continuo con PnL teórico > 0 — requiere deploy

## Gate F3 → F4 (código listo EN SECO, jamás encendido)

- [x] Executor postea sólo tras `check_and_reserve`; rollback de piernas; fills por user channel
- [x] Reconciliación on-chain↔DB con pausa preventiva y bloqueo del NO-GO
- [x] `TRADING_ENABLED=false` en todo el repo; encenderlo es acción humana
- [ ] Todo lo demás del gate (20 trades reales, kill-switch en vivo) — post-encendido humano

## Última iteración

2026-07-03 — Construcción completa F0→F3 en 4 commits (`533b11e`, `09a2ef1`,
`dae060f`, `3a449eb`): infra portada y adaptada a Polymarket, EIP-712 + WS +
data capture, motor 1 en shadow con funnel/analyst, F3 en seco. 149 tests,
ruff verde, smoke y NO-GO verdes localmente.

## Siguiente paso

1. **Humano:** merge del PR y deploy en Coolify como app nueva (§0.3): puerto
   host `:18081`, volúmenes `/app/data` `/app/logs` `/app/secrets`, env vars
   del panel (`.env.example` como plantilla), wallet key como secret volume.
2. Verificar `http://104.236.211.240:18081/health` y dejar el contenedor ≥ 1h
   (cierra el gate F0).
3. Con el deploy corriendo empieza el reloj de 48h de captura (gate F1) y luego
   los 7 días de shadow (gate F2). El digest de Telegram reporta el funnel; el
   veredicto diario queda en `analyst_verdicts`.
4. Cross-check pendiente de F1: validar la firma contra el API real
   (`derive_api_key` 200) con la wallet de prueba.

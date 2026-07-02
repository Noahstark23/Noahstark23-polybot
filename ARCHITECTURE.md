# POLYBOT — Arquitectura, Infraestructura & Plan de Construcción
### Documento maestro para Claude Code (modelo: Fable 5)

> Bot algorítmico para **Polymarket** que corre sobre la **misma infraestructura física**
> que `botkalshi` (Coolify en un droplet DigitalOcean), pero como **proyecto, repo y
> contenedor SEPARADOS y aislados**. Reutiliza ~70% del andamiaje del bot Kalshi
> (runner asyncio, risk manager, storage, health server, deploy) y reemplaza sólo la capa
> del venue (auth EVM, cliente CLOB REST/WS, modelo de mercado).
>
> **Repo base analizado:** `Noahstark23/botkalshi`. **Autor:** Noel Pineda.
> **Ventana de trabajo:** aprovechar Fable 5 hasta el 7 de junio.

---

## 0. INFRAESTRUCTURA — dónde corre esto (LEER PRIMERO)

### 0.1 La máquina (compartida, NO se crea nueva)

```
┌──────────────────────────────────────────────────────────────┐
│ DigitalOcean Droplet · IP 104.236.211.240 (ÚNICO, EXISTENTE) │
│                                                              │
│ Coolify v4.0.0 · panel en :8000 · server "localhost"         │
│ (Coolify despliega en la MISMA máquina donde corre)          │
│ Team: Root Team · Project: "My first project" (production)   │
│                                                              │
│ ┌────────────────────────────┐ ┌───────────────────────────┐ │
│ │ CONTENEDOR EXISTENTE       │ │ CONTENEDOR NUEVO (Polybot)│ │
│ │ kalshi-bot-rzd9wh8...      │ │ polybot-xxxx              │ │
│ │ health host :18080         │ │ health host :18081 ◄NEW   │ │
│ │ DB volume: kalshi data     │ │ DB volume: polybot data   │ │
│ │ secret: kalshi_private_key │ │ secret: poly_wallet_key   │ │
│ │ ── DINERO REAL, NO TOCAR ──│ │ ── aislado del anterior ──│ │
│ └────────────────────────────┘ └───────────────────────────┘ │
│        (comparten CPU/RAM/disco, procesos independientes)    │
└──────────────────────────────────────────────────────────────┘
```

### 0.2 Qué se CREA nuevo vs qué se COMPARTE

| Recurso | ¿Nuevo o compartido? | Detalle |
|---|---|---|
| Droplet DigitalOcean | **COMPARTIDO** | `104.236.211.240`. No se crea otro. |
| Instancia Coolify | **COMPARTIDO** | Panel `:8000`, server "localhost". |
| Proyecto Coolify | **COMPARTIDO** | "My first project" / entorno production. |
| **Repo GitHub** | **NUEVO** | p.ej. `Noahstark23/polybot` (privado). |
| **App / contenedor Coolify** | **NUEVO** | "Add Resource" → nueva app apuntando al repo Polybot. |
| **Puerto host del health** | **NUEVO** | `:18081` (el `:18080` ya lo usa Kalshi). |
| **DB SQLite (volumen)** | **NUEVO** | `/app/data` propio, aislado del de Kalshi. |
| **Logs (volumen)** | **NUEVO** | `/app/logs` propio. |
| **Secret (wallet key)** | **NUEVO** | secret volume propio; jamás compartido con Kalshi. |
| RiskManager / stop-loss | Código PORTADO, **estado aislado** | Cada bot tiene su propio kill-switch y su propia DB de riesgo. |

> **Regla de aislamiento (invariante):** un fallo, kill-switch, OOM o crash de Polybot
> **NO** debe poder afectar al contenedor Kalshi que ya opera con dinero real. Contenedores,
> volúmenes, puertos y secrets separados. Comparten sólo el hardware.

### 0.3 Pasos de provisión (orden exacto, hazlos tú — no el bot)
1. **GitHub:** crear repo privado nuevo `polybot`. (Crear cuentas/repos y dar permisos lo hace el humano, no el agente.)
2. **Coolify → Sources:** conectar el repo Polybot (o reutilizar la GitHub App/deploy key ya conectada, si aplica).
3. **Coolify → "My first project" → + Add Resource → Application** apuntando al repo Polybot, branch `main`.
4. **Puertos:** mapear health del contenedor `:8080` → host `:18081`.
5. **Volúmenes persistentes:** `/app/data`, `/app/logs`, `/app/secrets` (nuevos, propios).
6. **Secret:** subir la wallet key EVM como **secret volume** (nunca en `.env` en claro, nunca en el repo).
7. **Env vars:** cargar en el panel de Coolify (ver §4). `TRADING_ENABLED=false` al inicio.
8. **Deploy:** arranca en modo data-capture/shadow. Verificar `http://104.236.211.240:18081/health`.

### 0.4 Puertos en uso en el droplet (para no colisionar)

```
:8000  → Coolify (panel)            [existente]
:18080 → Kalshi bot health/status   [existente]
:18081 → Polybot health/status      [NUEVO - reservar este]
```

---

## 1. Contexto y filosofía heredada

Monolito asyncio en Python 3.12. Un `ProductionRunner` orquesta N "motores" (estrategias)
contra un venue. Cada motor: detecta oportunidad → `RiskManager` centralizado → `Executor`
→ persiste en SQLite (SQLModel). FastAPI expone health/status y admin de pausa. Deploy Docker
multi-stage en Coolify.

**Reglas duras que SE HEREDAN sin cambios (hardcoded, no se tocan sin aprobación explícita):**
- Stop-loss diario -3% → pausa 24h; semanal -8% → pausa 7d; mensual -15% → kill-switch total.
- Tope de exposición simultánea 25% del capital activo.
- Sizing máximo por trade 5% (¼ Kelly).
- **Paper/shadow-first: 7+ días antes de dinero real.**
- **CERO LLMs en el hot path de trading.**

---

## 2. Qué se REUTILIZA tal cual (portar primero)

| Módulo base | Rol | Cambio para Polybot |
|---|---|---|
| `src/runner.py` (`ProductionRunner`) | Orquestador asyncio, señales, digest de capital | Cambiar imports de motores; mantener esqueleto |
| `src/risk/manager.py` | Stop-loss multi-timeframe, kill-switch, exposición, capital dinámico | **Reutilizar íntegro** (opera en USD, agnóstico) |
| `src/storage/models.py` | `Trade`, `PortfolioPosition`, `RiskEvent`, `DailyPnL`, `BotRun`, `OperationalState`, `EdgeWindow`… | Reutilizar + campos Polymarket (§5) |
| `src/monitoring/health.py` | `/health`, `/ready`, `/status`, `/admin/pause\|resume` | Reutilizar |
| `src/monitoring/telegram_alerts.py`, `memory_monitor.py`, `dashboard.py` | Alertas/observabilidad | Reutilizar |
| `src/math/` (`kelly`, `arbitrage`, `no_vig`) | Matemática pura | Reutilizar; **`fees.py` se reescribe** |
| `src/utils/config.py` | Settings pydantic + validación producción | Reutilizar estructura; cambiar auth |
| `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml` | Build + deploy + CI | Reutilizar con ajustes menores |

---

## 3. Qué se REEMPLAZA (capa del venue)

| Concepto | Kalshi (base) | Polymarket (nuevo) |
|---|---|---|
| **Auth** | RSA-PSS signing, API Key ID + private key | Firma **EIP-712 / wallet EVM** (Polygon); credenciales L2 del CLOB. NO es RSA. |
| **Dinero** | USD fiat en cuenta Kalshi | **USDC on-chain (Polygon)**; approvals/allowances |
| **REST** | `src/clients/kalshi_rest.py` | `src/clients/polymarket_clob.py` (`clob.polymarket.com`) |
| **WS** | `src/clients/kalshi_ws.py` | `src/clients/polymarket_ws.py` (`market` + `user` channels) |
| **ID de mercado** | `ticker` | `condition_id` + `token_id` (ERC-1155) |
| **Precios** | 0–100¢, tick 1¢ | 0.0–1.0 USDC, tick variable por mercado |

> ⚠️ **Diferencia crítica:** el dinero es USDC on-chain. El bot firma **órdenes del CLOB**
> (mensajes EIP-712 off-chain), NO transacciones de custodia. Los **approvals de USDC /
> allowances los hace el humano una sola vez, JAMÁS el bot.**

---

## 4. Configuración (env vars)

**Reutilizadas del base:**

```
DATABASE_URL, LOG_LEVEL, TRADING_ENABLED,
MAX_DAILY_LOSS_PCT, MAX_WEEKLY_LOSS_PCT, MAX_MONTHLY_LOSS_PCT,
MAX_SIMULTANEOUS_EXPOSURE_PCT, MAX_TRADE_SIZE_PCT, KELLY_FRACTION,
MIN_EDGE_PCT, MIN_LIQUIDITY_CONTRACTS,
ACTIVE_CAPITAL_USD, DYNAMIC_CAPITAL_ENABLED, CAPITAL_SAFETY_FACTOR_PCT,
CAPITAL_FLOOR_USD, CAPITAL_CAP_USD, CAPITAL_SMOOTHING_PCT, BALANCE_REFRESH_SECONDS,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_DASHBOARD_ENABLED,
HEALTH_HOST, HEALTH_PORT, SENTRY_DSN
```

**Nuevas de Polymarket:**

```
POLYMARKET_ENV                            # paper | production
POLYGON_CHAIN_ID                          # 137
CLOB_API_URL                              # https://clob.polymarket.com
CLOB_WS_URL                               # wss://ws-subscriptions-clob.polymarket.com
POLY_WALLET_ADDRESS                       # dirección pública (NO la private key)
POLY_SIGNER_KEY_PATH                      # ruta al keystore cifrado (secret volume)
CLOB_API_KEY / CLOB_SECRET / CLOB_PASSPHRASE  # credenciales L2 (secrets)
USDC_CONTRACT                             # USDC.e Polygon
HEALTH_PORT=8080                          # dentro del contenedor (host lo mapea a :18081)
```

> 🔐 La private key del wallet **nunca** en `.env` en claro ni en el repo. Sólo secret volume.

---

## 5. Modelo de datos

Reutiliza tablas SQLModel del base. Ajustes:
- `Trade`: `ticker` → `condition_id` + `token_id` + `outcome`.
- `PortfolioPosition`: `token_id`, `avg_price` (0.0–1.0), `on_chain_synced` (bool).
- `MarketSnapshot`: `condition_id`, `question`, `end_date_iso`, `neg_risk`, `tick_size`.
- `RiskEvent` / kill-switch: **idénticos** al base.

---

## 6. MAPA COMPLETO DE FASES (F0 → F4)

> Convención heredada (`docs/motor_5_market_maker_plan_fases.md`): **los gates son la
> autoridad, las fechas son estimaciones.** Regla anti-Lección-9: nunca se avanza de fase
> por "vamos atrasados". Gate rojo → detiene la fase, se documenta la causa, se re-entra
> por el loop (§7). Fable 5 ejecuta el loop de cada fase.

### FASE 0 — Scaffold & Discovery  `[read-only, sin dinero]`
**Objetivo:** contenedor arranca en Coolify (`:18081/health`), conecta al CLOB en lectura,
infra portada funciona. Cero trading.
**Archivos:** portar infra (§2) + stubs `NotImplementedError` de auth/clients; `smoke_test.py`, `check_no_go.py`.
**Instrucciones del loop:**
1. `ProductionRunner` arranca sin motores y sirve health.
2. Sólo lecturas públicas del CLOB: `get_markets`, `get_orderbook`, `get_market`.
3. `smoke_test.py`: conecta CLOB, lista ≥1 mercado, lee 1 orderbook, abre DB, levanta health, exit 0.
4. Deploy como app NUEVA en Coolify, host `:18081`, volúmenes propios.

**Gate F0 → F1:**
- [ ] `docker build` ok; contenedor `healthy` ≥ 1h sin reinicios.
- [ ] `GET /health` 200; `GET /status` JSON con `started_at`, `ws_connected:false`.
- [ ] `smoke_test.py` verde en CI; `pytest` de `config` y `risk/manager` al 100%.
- [ ] `.pem`/keystore fuera del repo; `config/` en `.gitignore` + `.dockerignore`.

### FASE 1 — Data Capture & Auth  `[read-only + firma, sin órdenes]`
**Objetivo:** stream WS→SQLite + firma EIP-712 funcional (firma pero NO postea).
**Archivos:** `auth/eip712_signer.py`, `clients/polymarket_ws.py`, `strategies/data_capture.py`;
en `polymarket_clob.py` añadir `get_balance_allowance`, `derive_api_key`, `build_order` (firma, no postea).
**Instrucciones del loop:**
1. Firma EIP-712 validada contra vector fijo (hash/firma esperados).
2. Derivar credenciales L2 desde la firma del wallet (key desde secret volume).
3. WS `market` channel + `OrderbookManager` por `token_id` con detección de gaps (portar `orderbook_manager_v2`).
4. `data_capture` persiste `MarketSnapshot` + `OrderbookEvent` por ciclo.
5. Instrumentar: reconexiones WS, gaps/60s, latencia de snapshot.

**Gate F1 → F2 (≥ 48h captura continua):**
- [ ] Firma EIP-712 validada; `derive_api_key` devuelve credenciales usables.
- [ ] WS estable ≥ 48h, 0 crashes, gaps/60s ≈ 0.
- [ ] DB con snapshots coherentes (`end_date_iso`, `tick_size`, `neg_risk`).
- [ ] `get_balance_allowance` correcto en wallet de prueba.
- [ ] `build_order` produce órdenes firmadas válidas, **sin postear**.

### FASE 2 — Motor 1 en SHADOW  `[detecta, NO ejecuta]`
**Objetivo:** arbitraje intra-mercado detecta edges y los registra en `EdgeWindow`, sin una sola orden.
**Archivos:** `motor_1_arbitrage/engine.py`, `orderbook_manager_v2.py`; `math/fees.py` **reescrito**;
`analytics/analyst_loop.py` + agregador de funnel.
**Instrucciones del loop:**
1. `engine._tick()`: edge = 1.0 − (best_ask_YES + best_ask_NO) − costos.
2. **Filtro anti-edge-fantasma desde el día 1:** si `edge > MIN_EDGE_PCT_MAX` (p.ej. 10%),
   loguear `edge_too_high` y NO ejecutar (lección directa del bot Kalshi).
3. Registrar edges válidos en `EdgeWindow` (timestamp, tokens, tamaño, costos).
4. `RiskManager.check_pre_trade()` en **dry-run** (registra decisión, no reserva capital).
5. Emitir `PolyFunnelSnapshot` por ciclo (evaluados, skips por causa, edges, PnL teórico).

**Gate F2 → F3 (≥ 7 días shadow continuo):**
- [ ] Fill-rate teórico y slippage dentro de rangos definidos.
- [ ] PnL neto teórico > 0 tras costos reales.
- [ ] `fees.py` validado contra costos reales del orderbook.
- [ ] Filtro anti-fantasma demostró bloquear edges irreales (logs `edge_too_high`).
- [ ] RiskManager dry-run disparó correctamente en escenarios simulados.
- [ ] `analyst_loop` produce `AnalystVerdict` diario comparable.

### FASE 3 — Ejecución en producción, capital mínimo  `[dinero real, tope duro]`
**Objetivo:** órdenes reales con capital mínimo y todos los límites activos. Validar firma→post→fill→reconciliación.
**Archivos:** `motor_1_arbitrage/executor.py` (postea firmadas); `polymarket_ws.py` `user` channel; reconciliación on-chain↔DB.
**Instrucciones del loop:**
1. `TRADING_ENABLED=true`, `POLYMARKET_ENV=production`, `ACTIVE_CAPITAL_USD` mínimo ($50–100). Límites sin diluir.
2. `Executor.execute()` sólo tras `RiskManager.check_and_reserve()` aprobado.
3. WS `user` channel confirma fills → `Trade` + `PortfolioPosition`.
4. Reconciliación cada N min: posiciones on-chain vs DB; discrepancia → alerta + pausa preventiva.
5. Kill-switch y stop-loss **verificados en vivo** (forzar límite bajo y confirmar que la pausa detiene ejecución).

**Gate F3 → F4:**
- [ ] ≥ 20 trades reales reconciliados sin discrepancia posición/DB.
- [ ] Kill-switch probado en vivo detiene la ejecución de forma verificable.
- [ ] PnL real converge con el teórico shadow dentro de tolerancia.
- [ ] Cero órdenes malformadas; latencia post-orden dentro de presupuesto.

### FASE 4 — Escalado & motores adicionales  `[opt-in, condicionado]`
**Objetivo:** subir capital escalonado y, si aplica, activar cross-venue Kalshi↔Polymarket
(referencia: `docs/motor_4_cross_venue_fase0.md` del repo base).
**Instrucciones del loop:**
1. Escalar `ACTIVE_CAPITAL_USD` sólo por config, tras N días verdes (definir con datos del funnel).
2. Cross-venue: arrancar en Fase 0 read-only de ese doc; medir el **riesgo #1 (resolución divergente)** antes de ejecutar.
3. Cada motor nuevo re-entra por F2→F3→F4. No se saltan fases.

**Gate permanente:** stop-loss mensual o discrepancia de reconciliación → revierte a capital mínimo y congela escalado hasta revisión humana.

---

## 7. EL LOOP DE INGENIERÍA (transversal a todas las fases)

Instrumentar ANTES de actuar:

```
OBSERVAR  PolyFunnelSnapshot por ciclo (evaluados, skips por causa, edges,
          fills, spread, exposición, PnL neto teórico y real)
ANALIZAR  analyst_loop con agregador de funnel (funciones PURAS, sin LLM en hot path)
RECORDAR  AnalystVerdict comparable día a día
REPORTAR  digest Telegram — el humano lee, decide, recalibra
ITERAR    cambios de parámetros SOLO por config (Coolify env vars),
          justificados con el dato
```

**Cómo Fable 5 ejecuta el loop:**
1. Leer el gate de la fase actual y los `PolyFunnelSnapshot` acumulados.
2. Editar SOLO los archivos de la fase; no tocar `risk/manager.py` ni límites hardcoded sin instrucción humana.
3. Correr `pytest` + `smoke_test.py` + `check_no_go.py` antes de proponer deploy.
4. Nunca avanzar de fase con gate rojo (anti-Lección-9).
5. Al cerrar iteración: actualizar `docs/handoff/` con estado, gate y causa si rojo.

---

## 8. CHECKLIST NO-GO (bloquea arranque con dinero)

`scripts/check_no_go.py` falla el arranque si:
- [ ] La wallet key no está en el secret volume (o está en `.env` en claro).
- [ ] `POLYMARKET_ENV=production` sin ≥ 7 días de shadow (F2).
- [ ] Kill-switch activo sin `clear_kill_switch.py` humano.
- [ ] Discrepancia posición on-chain ↔ DB en el último ciclo.
- [ ] Algún límite de riesgo fuera del rango hardcoded permitido.
- [ ] `TRADING_ENABLED=true` sin `ACTIVE_CAPITAL_USD` configurado.
- [ ] Puerto `:18081` o volúmenes colisionando con el contenedor Kalshi.

---

## 9. INVARIANTES DE SEGURIDAD (no negociables)
- Aislamiento total del contenedor/DB/secret respecto al bot Kalshi (mismo hardware, procesos separados).
- Paper/shadow ≥ 7 días antes de dinero real.
- Cero LLMs en el hot path de trading.
- Approvals de USDC / allowances / cualquier tx de custodia: las hace el humano, jamás el bot.
- Private key del wallet: nunca en repo, nunca en `.env` en claro, sólo secret volume.
- RiskManager y kill-switch portados sin diluir ningún límite.
- Filtro anti-edge-fantasma activo desde el día 1.

---

## 10. HALLAZGO DE SEGURIDAD EN EL REPO BASE (accionar antes de clonar el patrón)

El repo `botkalshi` tiene `config/kalshi_private_key.pem` **commiteado** y el Dockerfile hace
`COPY config/ ./config/`. Si esa llave es real es una fuga de credenciales. Para Polybot:
la wallet key **jamás** se commitea; sólo secret volume; `config/` sensible en `.gitignore`
+ `.dockerignore`. **Recomendación:** rotar la API key de Kalshi y purgar el `.pem` del
historial de git.

---

## Notas del autor del documento

- Los detalles del CLOB de Polymarket (endpoints, flujo EIP-712) reflejan conocimiento hasta
  enero 2025 — **verificar contra la doc oficial vigente al implementar.**
- El "encender trading" (`TRADING_ENABLED=true` con `POLYMARKET_ENV=production`) y los
  approvals de USDC on-chain los hace el humano, no el agente ni el bot — están fuera del
  sprint a propósito.

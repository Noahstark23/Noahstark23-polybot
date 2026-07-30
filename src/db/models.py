"""
Modelos SQLModel de Polybot (ARCHITECTURE.md §5).

Reutiliza el schema del bot base con la capa Polymarket:
    - Identidad de mercado: `condition_id` + `token_id` (ERC-1155) + `outcome`.
    - Precios en USDC 0.0–1.0 (no centavos).

Tablas:
    trades              cada intento de trade (shadow o real)
    positions           exposición viva / historial (con on_chain_synced)
    market_snapshots    metadata de mercado por ciclo de captura
    orderbook_events    eventos del WS market channel
    edge_windows        edges detectados por el motor (F2, shadow)
    funnel_snapshots    PolyFunnelSnapshot por ciclo del motor
    analyst_verdicts    veredicto diario del analyst_loop
    risk_events         eventos del risk manager (pausas, kill-switch)
    daily_pnl           snapshot diario para reconciliación
    bot_runs            tracking de cada arranque
    operational_state   estado clave/valor (kill_switch, paused_until, ...)
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


class Trade(SQLModel, table=True):
    """Registro de un trade (real en F3+; en F2 el motor NO escribe aquí)."""

    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)

    # Identificadores Polymarket
    client_order_id: str = Field(unique=True, max_length=100)
    exchange_order_id: str | None = Field(default=None, max_length=100)
    condition_id: str = Field(index=True, max_length=100)
    token_id: str = Field(index=True, max_length=100)
    outcome: str = Field(max_length=10)  # "YES" / "NO"

    # Semántica
    side: str = Field(max_length=10)  # "buy" / "sell"
    size: float  # contratos (Polymarket permite fraccionales)
    price: float  # USDC 0.0–1.0

    # Origen
    strategy: str = Field(index=True, max_length=50)
    estimated_edge_pct: float | None = None

    # Estado: pending -> placed -> filled -> settled | cancelled | error
    status: str = Field(default="pending", max_length=20)

    # Resultado (post-fill)
    fill_price: float | None = None
    fill_size: float | None = None
    fees_usd: float | None = None
    pnl_usd: float | None = None

    placed_at: datetime = Field(default_factory=utc_now, index=True)
    filled_at: datetime | None = None
    settled_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=500)


class Position(SQLModel, table=True):
    """Posición viva / historial de exposición."""

    __tablename__ = "positions"

    id: int | None = Field(default=None, primary_key=True)
    condition_id: str = Field(index=True, max_length=100)
    token_id: str = Field(index=True, max_length=100)
    outcome: str = Field(max_length=10)

    contracts: float
    avg_price: float  # USDC 0.0–1.0
    exposure_usd: float
    on_chain_synced: bool = False  # reconciliación on-chain <-> DB (F3)

    strategy: str = Field(index=True, max_length=50)

    closed_at: datetime | None = Field(default=None, index=True)
    close_price: float | None = None
    realized_pnl_usd: float | None = None

    opened_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)


class MarketSnapshot(SQLModel, table=True):
    """Metadata de un mercado capturada por ciclo (F1 data capture)."""

    __tablename__ = "market_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    condition_id: str = Field(index=True, max_length=100)
    question: str = Field(max_length=500)
    end_date_iso: str | None = Field(default=None, max_length=40)
    neg_risk: bool = False
    tick_size: float = 0.01
    token_id_yes: str = Field(max_length=100)
    token_id_no: str = Field(max_length=100)
    best_ask_yes: float | None = None
    best_ask_no: float | None = None
    best_bid_yes: float | None = None
    best_bid_no: float | None = None
    active: bool = True
    captured_at: datetime = Field(default_factory=utc_now, index=True)


class OrderbookEvent(SQLModel, table=True):
    """Evento del WS market channel (book / price_change / tick_size_change)."""

    __tablename__ = "orderbook_events"

    id: int | None = Field(default=None, primary_key=True)
    token_id: str = Field(index=True, max_length=100)
    event_type: str = Field(max_length=30)
    best_bid: float | None = None
    best_ask: float | None = None
    bid_depth: float | None = None  # contratos al mejor bid
    ask_depth: float | None = None
    book_hash: str | None = Field(default=None, max_length=100)
    is_gap: bool = False  # detección de gaps (hash no matchea el esperado)
    received_at: datetime = Field(default_factory=utc_now, index=True)


class EdgeWindow(SQLModel, table=True):
    """Edge detectado por el motor 1 (F2 shadow: se registra, NO se ejecuta)."""

    __tablename__ = "edge_windows"

    id: int | None = Field(default=None, primary_key=True)
    condition_id: str = Field(index=True, max_length=100)
    token_id_yes: str = Field(max_length=100)
    token_id_no: str = Field(max_length=100)

    best_ask_yes: float
    best_ask_no: float
    gross_edge_pct: float  # (1 - ask_yes - ask_no) * 100, sin costos
    costs_pct: float  # fees + slippage estimado
    net_edge_pct: float  # gross - costs

    max_size_contracts: float  # liquidez disponible en los mejores asks
    theoretical_size: float  # size que el sizing habría usado
    theoretical_pnl_usd: float  # net_edge * theoretical_size

    # Resultado del pipeline shadow
    status: str = Field(index=True, max_length=30)
    # detected | edge_too_high (anti-fantasma) | below_min_edge |
    # low_liquidity | risk_blocked | shadow_recorded | executed (F3)
    risk_approved: bool | None = None
    risk_reason: str | None = Field(default=None, max_length=300)

    detected_at: datetime = Field(default_factory=utc_now, index=True)


class MultiEdgeWindow(SQLModel, table=True):
    """
    Edge multi-outcome detectado por el Motor 2 (neg-risk, F2 shadow).

    TABLA PROPIA, no una fila más de edge_windows con un discriminador
    (lección Kalshi 2026-07-28: edge_pct polimórfica — %, z-scores y centavos
    en la misma columna — produjo "max 2678pp" y 1349 falsos sospechosos).
    Acá hay UNA unidad: todos los *_pct son % del capital comprometido por set.
    """

    __tablename__ = "multi_edge_windows"

    id: int | None = Field(default=None, primary_key=True)
    neg_risk_market_id: str = Field(index=True, max_length=100)
    direction: str = Field(max_length=15)  # buy_yes_all | buy_no_all
    legs: int  # cantidad de outcomes del set
    legs_json: str | None = None  # JSON [{condition_id, ask}] — reconstrucción exacta

    cost_per_set: float  # USDC por set (suma de asks)
    payout_per_set: float  # 1.0 (buy_yes_all) o N-1 (buy_no_all)
    fees_per_set: float  # fees CLOB + slippage, todas las patas
    gross_edge_pct: float  # (payout - cost) / cost * 100
    net_edge_pct: float  # (payout - cost - fees) / cost * 100

    min_leg_depth_contracts: float  # liquidez de la pata MÁS FINA (manda ella)
    theoretical_size_sets: float  # sets que el sizing habría comprado
    theoretical_pnl_usd: float

    # Mismo pipeline shadow que motor 1
    status: str = Field(index=True, max_length=30)
    # detected | edge_too_high | below_min_edge | low_liquidity |
    # risk_blocked | shadow_recorded
    risk_approved: bool | None = None
    risk_reason: str | None = Field(default=None, max_length=300)

    detected_at: datetime = Field(default_factory=utc_now, index=True)


class FunnelSnapshot(SQLModel, table=True):
    """PolyFunnelSnapshot: métricas del funnel por ciclo del motor (§7)."""

    __tablename__ = "funnel_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    # Qué motor emitió el ciclo (lección Kalshi: el agregado ENMASCARA — la
    # auditoría 07-18 encontró M2 -$432 escondido detrás de un neto "aceptable").
    # Las filas pre-migración quedan con el default = motor_1.
    motor: str = Field(default="motor_1", index=True, max_length=20)
    cycle_ts: datetime = Field(default_factory=utc_now, index=True)
    markets_evaluated: int = 0
    skips_json: str | None = None  # JSON {causa: count}
    edges_detected: int = 0
    edges_phantom: int = 0  # bloqueados por anti-fantasma
    edges_risk_blocked: int = 0
    edges_recorded: int = 0  # registrados en EdgeWindow como shadow_recorded
    theoretical_pnl_usd: float = 0.0
    exposure_pct: float = 0.0
    ws_connected: bool = False
    cycle_latency_ms: float | None = None


class AnalystVerdict(SQLModel, table=True):
    """Veredicto diario comparable del analyst_loop (§7, funciones puras)."""

    __tablename__ = "analyst_verdicts"

    id: int | None = Field(default=None, primary_key=True)
    date: str = Field(unique=True, index=True, max_length=10)  # YYYY-MM-DD
    verdict: str = Field(max_length=20)  # healthy | degraded | no_data
    summary: str = Field(max_length=2000)
    metrics_json: str | None = None
    generated_at: datetime = Field(default_factory=utc_now)


class RiskEvent(SQLModel, table=True):
    """Eventos del risk manager (idéntico al base)."""

    __tablename__ = "risk_events"

    id: int | None = Field(default=None, primary_key=True)
    event_type: str = Field(index=True, max_length=50)
    # daily_loss_limit, weekly_loss_limit, monthly_loss_limit,
    # exposure_limit, kill_switch, manual_pause, reconcile_mismatch, ...
    severity: str = Field(max_length=20)  # info / warning / critical
    message: str = Field(max_length=1000)
    capital_at_event: float | None = None
    triggered_at: datetime = Field(default_factory=utc_now, index=True)


class DailyPnL(SQLModel, table=True):
    """Resumen diario de PnL (una fila por día)."""

    __tablename__ = "daily_pnl"

    id: int | None = Field(default=None, primary_key=True)
    date: str = Field(unique=True, index=True, max_length=10)  # YYYY-MM-DD
    starting_capital: float
    ending_capital: float
    pnl: float
    pnl_pct: float
    trades_count: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    strategy_breakdown: str | None = None  # JSON


class BotRun(SQLModel, table=True):
    """Tracking de cada arranque del bot."""

    __tablename__ = "bot_runs"

    id: int | None = Field(default=None, primary_key=True)
    environment: str = Field(max_length=20)  # paper / production
    trading_enabled: bool = False
    capital_at_start: float
    motors_enabled: str | None = Field(default=None, max_length=500)  # JSON list
    started_at: datetime = Field(default_factory=utc_now, index=True)
    ended_at: datetime | None = None
    crash_reason: str | None = Field(default=None, max_length=500)


class OperationalState(SQLModel, table=True):
    """Estado operacional clave/valor persistente (kill_switch, pausas)."""

    __tablename__ = "operational_state"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True, max_length=50)
    value: str = Field(max_length=1000)
    updated_at: datetime = Field(default_factory=utc_now)

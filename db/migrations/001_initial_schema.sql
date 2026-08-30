-- File: db/migrations/001_initial_schema.sql
-- Purpose: Initial DELTAX v2 PostgreSQL/Neon database schema
-- Important: Run this migration only once.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- =========================================================
-- MIGRATION HISTORY
-- =========================================================

CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =========================================================
-- STRATEGY CONFIGURATION
-- =========================================================

CREATE TABLE strategy_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    config JSONB NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX uq_single_active_strategy
ON strategy_configs ((1))
WHERE is_active = true;


-- =========================================================
-- TRADING UNIVERSE
-- =========================================================

CREATE TABLE instruments (
    symbol TEXT PRIMARY KEY,
    alpaca_symbol TEXT NOT NULL UNIQUE,
    company_name TEXT,
    asset_type TEXT NOT NULL DEFAULT 'stock'
        CHECK (asset_type IN ('stock', 'etf')),

    sector TEXT,
    industry TEXT,
    sector_proxy_symbol TEXT REFERENCES instruments(symbol),
    cik TEXT,

    is_trade_candidate BOOLEAN NOT NULL DEFAULT true,
    is_market_proxy BOOLEAN NOT NULL DEFAULT false,

    stock_enabled BOOLEAN NOT NULL DEFAULT true,
    options_enabled BOOLEAN NOT NULL DEFAULT false,

    alpaca_tradable BOOLEAN,
    alpaca_shortable BOOLEAN,
    alpaca_easy_to_borrow BOOLEAN,
    alpaca_fractionable BOOLEAN,
    alpaca_marginable BOOLEAN,

    last_validated_at TIMESTAMPTZ,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =========================================================
-- EARNINGS CALENDAR
-- =========================================================

CREATE TABLE earnings_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    symbol TEXT NOT NULL REFERENCES instruments(symbol),

    report_date DATE NOT NULL,

    report_time TEXT NOT NULL DEFAULT 'unknown'
        CHECK (
            report_time IN (
                'before_open',
                'after_close',
                'during_market',
                'unknown'
            )
        ),

    fiscal_period_end DATE,

    estimated_eps NUMERIC(20, 8),
    reported_eps NUMERIC(20, 8),

    estimated_revenue NUMERIC(24, 4),
    reported_revenue NUMERIC(24, 4),

    currency TEXT,

    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (
            status IN (
                'scheduled',
                'reported',
                'cancelled',
                'unknown'
            )
        ),

    source TEXT NOT NULL,
    source_external_id TEXT,

    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (symbol, report_date, source)
);


-- =========================================================
-- RAW NEWS, SEC FILINGS AND SOURCE EVENTS
-- =========================================================

CREATE TABLE source_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source_type TEXT NOT NULL,

    headline TEXT,
    summary TEXT,
    content TEXT,
    source_url TEXT,

    published_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    content_hash TEXT,

    processing_status TEXT NOT NULL DEFAULT 'pending',

    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,

    UNIQUE (source, external_id)
);

CREATE TABLE source_event_symbols (
    source_event_id UUID NOT NULL
        REFERENCES source_events(id)
        ON DELETE CASCADE,

    symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    PRIMARY KEY (source_event_id, symbol)
);


-- =========================================================
-- EVENT GROUPING AND DEDUPLICATION
-- =========================================================

CREATE TABLE event_clusters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    cluster_key TEXT NOT NULL UNIQUE,

    primary_symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    event_type TEXT,

    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed')),

    first_published_at TIMESTAMPTZ NOT NULL,
    last_published_at TIMESTAMPTZ NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_cluster_members (
    event_cluster_id UUID NOT NULL
        REFERENCES event_clusters(id)
        ON DELETE CASCADE,

    source_event_id UUID NOT NULL
        REFERENCES source_events(id)
        ON DELETE CASCADE,

    PRIMARY KEY (event_cluster_id, source_event_id)
);


-- =========================================================
-- OPENAI ANALYSIS
-- =========================================================

CREATE TABLE ai_analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    source_event_id UUID
        REFERENCES source_events(id),

    event_cluster_id UUID
        REFERENCES event_clusters(id),

    symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (
            status IN (
                'queued',
                'running',
                'completed',
                'failed',
                'refused'
            )
        ),

    event_type TEXT,

    direction TEXT
        CHECK (
            direction IN (
                'bullish',
                'bearish',
                'neutral'
            )
        ),

    impact_score SMALLINT
        CHECK (impact_score BETWEEN -100 AND 100),

    confidence NUMERIC(5, 4)
        CHECK (confidence BETWEEN 0 AND 1),

    time_horizon TEXT
        CHECK (
            time_horizon IN (
                'intraday',
                'active',
                'core',
                'unknown'
            )
        ),

    trade_relevance NUMERIC(5, 4)
        CHECK (trade_relevance BETWEEN 0 AND 1),

    source_quality NUMERIC(5, 4)
        CHECK (source_quality BETWEEN 0 AND 1),

    catalyst TEXT,

    facts JSONB NOT NULL DEFAULT '[]'::jsonb,
    risks JSONB NOT NULL DEFAULT '[]'::jsonb,

    invalidation_condition TEXT,

    earnings_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,

    raw_response JSONB,
    error_message TEXT,

    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,

    CHECK (
        num_nonnulls(source_event_id, event_cluster_id) = 1
    )
);

CREATE UNIQUE INDEX uq_ai_source_event_analysis
ON ai_analyses (
    source_event_id,
    symbol,
    model,
    prompt_version,
    input_hash
)
WHERE source_event_id IS NOT NULL;

CREATE UNIQUE INDEX uq_ai_cluster_analysis
ON ai_analyses (
    event_cluster_id,
    symbol,
    model,
    prompt_version,
    input_hash
)
WHERE event_cluster_id IS NOT NULL;


-- =========================================================
-- FIVE-MINUTE SCANNER RUNS
-- =========================================================

CREATE TABLE scan_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    strategy_config_id UUID NOT NULL
        REFERENCES strategy_configs(id),

    scanner_name TEXT NOT NULL,
    scheduled_for TIMESTAMPTZ NOT NULL,

    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,

    status TEXT NOT NULL DEFAULT 'running'
        CHECK (
            status IN (
                'running',
                'completed',
                'partial',
                'failed',
                'skipped'
            )
        ),

    market_open BOOLEAN,

    symbols_requested INTEGER NOT NULL DEFAULT 0,
    symbols_processed INTEGER NOT NULL DEFAULT 0,
    signals_found INTEGER NOT NULL DEFAULT 0,

    error_message TEXT,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    UNIQUE (scanner_name, scheduled_for)
);


-- =========================================================
-- MARKET SNAPSHOTS
-- =========================================================

CREATE TABLE market_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    scan_run_id UUID
        REFERENCES scan_runs(id)
        ON DELETE SET NULL,

    symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    feed TEXT NOT NULL,

    captured_at TIMESTAMPTZ NOT NULL,

    quote_timestamp TIMESTAMPTZ,
    trade_timestamp TIMESTAMPTZ,

    latest_price NUMERIC(20, 8),

    bid_price NUMERIC(20, 8),
    ask_price NUMERIC(20, 8),

    bid_size NUMERIC(20, 8),
    ask_size NUMERIC(20, 8),

    minute_open NUMERIC(20, 8),
    minute_high NUMERIC(20, 8),
    minute_low NUMERIC(20, 8),
    minute_close NUMERIC(20, 8),
    minute_volume BIGINT,

    daily_vwap NUMERIC(20, 8),
    daily_volume BIGINT,

    previous_close NUMERIC(20, 8),

    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,

    UNIQUE (symbol, feed, captured_at)
);


-- =========================================================
-- TRADING THESES AND SIGNAL STATE MACHINE
-- =========================================================

CREATE TABLE trade_theses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    scan_run_id UUID NOT NULL
        REFERENCES scan_runs(id),

    strategy_config_id UUID NOT NULL
        REFERENCES strategy_configs(id),

    symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    strategy TEXT NOT NULL
        CHECK (
            strategy IN (
                'core',
                'active',
                'intraday'
            )
        ),

    direction TEXT NOT NULL
        CHECK (
            direction IN (
                'long',
                'short'
            )
        ),

    status TEXT NOT NULL DEFAULT 'detected'
        CHECK (
            status IN (
                'detected',
                'awaiting_ai',
                'awaiting_confirmation',
                'approved',
                'rejected',
                'expired',
                'intents_created'
            )
        ),

    ai_analysis_id UUID
        REFERENCES ai_analyses(id),

    signal_at TIMESTAMPTZ NOT NULL,
    signal_price NUMERIC(20, 8) NOT NULL,

    reference_vwap NUMERIC(20, 8),
    deviation_pct NUMERIC(10, 6),

    atr_14 NUMERIC(20, 8),
    atr_pct NUMERIC(10, 6),

    weak_indices_count SMALLINT,

    technical_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    market_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    sector_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk_state JSONB NOT NULL DEFAULT '{}'::jsonb,

    confirmation_due_at TIMESTAMPTZ,
    confirmation_checked_at TIMESTAMPTZ,
    confirmation_price NUMERIC(20, 8),
    confirmation_passed BOOLEAN,

    expires_at TIMESTAMPTZ NOT NULL,

    rejection_reasons TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (
        scan_run_id,
        symbol,
        strategy,
        direction
    )
);


-- =========================================================
-- OPTION QUOTES AND GREEKS
-- =========================================================

CREATE TABLE option_quote_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    scan_run_id UUID
        REFERENCES scan_runs(id),

    trade_thesis_id UUID
        REFERENCES trade_theses(id),

    underlying_symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    contract_symbol TEXT NOT NULL,

    option_type TEXT NOT NULL
        CHECK (option_type IN ('call', 'put')),

    expiration_date DATE NOT NULL,

    dte INTEGER NOT NULL
        CHECK (dte >= 0),

    strike NUMERIC(20, 8) NOT NULL,

    multiplier INTEGER NOT NULL DEFAULT 100
        CHECK (multiplier > 0),

    bid_price NUMERIC(20, 8),
    ask_price NUMERIC(20, 8),
    mid_price NUMERIC(20, 8),
    last_price NUMERIC(20, 8),

    volume BIGINT,
    open_interest BIGINT,

    implied_volatility NUMERIC(16, 10),

    delta NUMERIC(16, 10),
    gamma NUMERIC(16, 10),
    theta NUMERIC(16, 10),
    vega NUMERIC(16, 10),
    rho NUMERIC(16, 10),

    quote_timestamp TIMESTAMPTZ,

    feed TEXT NOT NULL,

    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);


-- =========================================================
-- TRADE INTENTS
-- =========================================================

CREATE TABLE trade_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    trade_thesis_id UUID NOT NULL
        REFERENCES trade_theses(id),

    strategy_config_id UUID NOT NULL
        REFERENCES strategy_configs(id),

    intent_type TEXT NOT NULL
        CHECK (
            intent_type IN (
                'entry',
                'exit',
                'emergency_exit'
            )
        ),

    asset_class TEXT NOT NULL
        CHECK (
            asset_class IN (
                'stock',
                'option_spread'
            )
        ),

    strategy TEXT NOT NULL
        CHECK (
            strategy IN (
                'core',
                'active',
                'intraday'
            )
        ),

    direction TEXT NOT NULL
        CHECK (
            direction IN (
                'long',
                'short'
            )
        ),

    symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    side TEXT
        CHECK (side IN ('buy', 'sell')),

    quantity NUMERIC(20, 8),

    order_type TEXT NOT NULL DEFAULT 'limit',
    time_in_force TEXT NOT NULL,

    limit_price NUMERIC(20, 8),
    planned_entry_price NUMERIC(20, 8),

    stop_loss_price NUMERIC(20, 8),
    take_profit_price NUMERIC(20, 8),

    trailing_activation_price NUMERIC(20, 8),
    trailing_distance_pct NUMERIC(10, 6),

    premium_type TEXT
        CHECK (
            premium_type IN (
                'credit',
                'debit',
                'none'
            )
        ),

    net_premium NUMERIC(20, 8),

    max_profit NUMERIC(20, 8),

    max_loss NUMERIC(20, 8) NOT NULL
        CHECK (max_loss >= 0),

    idempotency_key TEXT NOT NULL UNIQUE,

    status TEXT NOT NULL DEFAULT 'created'
        CHECK (
            status IN (
                'created',
                'approved',
                'submitting',
                'submitted',
                'partially_filled',
                'filled',
                'cancelled',
                'rejected',
                'expired',
                'failed'
            )
        ),

    expires_at TIMESTAMPTZ NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    CHECK (
        asset_class <> 'option_spread'
        OR strategy IN ('core', 'active')
    )
);

CREATE TABLE trade_intent_legs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    trade_intent_id UUID NOT NULL
        REFERENCES trade_intents(id)
        ON DELETE CASCADE,

    leg_number SMALLINT NOT NULL
        CHECK (leg_number > 0),

    option_quote_snapshot_id UUID
        REFERENCES option_quote_snapshots(id),

    contract_symbol TEXT NOT NULL,

    action TEXT NOT NULL
        CHECK (
            action IN (
                'buy_to_open',
                'sell_to_open',
                'buy_to_close',
                'sell_to_close'
            )
        ),

    ratio_quantity INTEGER NOT NULL DEFAULT 1
        CHECK (ratio_quantity > 0),

    option_type TEXT NOT NULL
        CHECK (option_type IN ('call', 'put')),

    strike NUMERIC(20, 8) NOT NULL,

    expiration_date DATE NOT NULL,

    multiplier INTEGER NOT NULL DEFAULT 100
        CHECK (multiplier > 0),

    reference_bid NUMERIC(20, 8),
    reference_ask NUMERIC(20, 8),
    reference_mid NUMERIC(20, 8),

    UNIQUE (trade_intent_id, leg_number)
);


-- =========================================================
-- ALPACA ORDERS
-- =========================================================

CREATE TABLE broker_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    trade_intent_id UUID NOT NULL
        REFERENCES trade_intents(id),

    alpaca_order_id TEXT UNIQUE,
    client_order_id TEXT NOT NULL UNIQUE,
    parent_alpaca_order_id TEXT,

    asset_class TEXT NOT NULL,
    order_class TEXT,
    order_type TEXT NOT NULL,
    time_in_force TEXT NOT NULL,

    side TEXT,
    quantity NUMERIC(20, 8),
    limit_price NUMERIC(20, 8),

    status TEXT NOT NULL,

    filled_quantity NUMERIC(20, 8) NOT NULL DEFAULT 0,
    filled_average_price NUMERIC(20, 8),

    submitted_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,

    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE broker_order_legs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    broker_order_id UUID NOT NULL
        REFERENCES broker_orders(id)
        ON DELETE CASCADE,

    alpaca_leg_order_id TEXT,

    contract_symbol TEXT NOT NULL,

    side TEXT NOT NULL,

    ratio_quantity INTEGER NOT NULL DEFAULT 1,

    status TEXT,

    filled_quantity NUMERIC(20, 8) NOT NULL DEFAULT 0,
    filled_average_price NUMERIC(20, 8),

    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,

    UNIQUE (
        broker_order_id,
        contract_symbol,
        side
    )
);

CREATE TABLE broker_order_events (
    id BIGSERIAL PRIMARY KEY,

    broker_order_id UUID NOT NULL
        REFERENCES broker_orders(id)
        ON DELETE CASCADE,

    event_type TEXT NOT NULL,

    broker_event_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    payload JSONB NOT NULL
);


-- =========================================================
-- POSITIONS
-- =========================================================

CREATE TABLE positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    trade_thesis_id UUID NOT NULL
        REFERENCES trade_theses(id),

    entry_intent_id UUID NOT NULL
        REFERENCES trade_intents(id),

    symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    asset_class TEXT NOT NULL
        CHECK (
            asset_class IN (
                'stock',
                'option_spread'
            )
        ),

    strategy TEXT NOT NULL
        CHECK (
            strategy IN (
                'core',
                'active',
                'intraday'
            )
        ),

    direction TEXT NOT NULL
        CHECK (
            direction IN (
                'long',
                'short'
            )
        ),

    status TEXT NOT NULL DEFAULT 'opening'
        CHECK (
            status IN (
                'opening',
                'open',
                'closing',
                'closed',
                'error'
            )
        ),

    quantity NUMERIC(20, 8),

    average_entry_price NUMERIC(20, 8),
    current_price NUMERIC(20, 8),

    stop_loss_price NUMERIC(20, 8),
    take_profit_price NUMERIC(20, 8),

    trailing_active BOOLEAN NOT NULL DEFAULT false,
    trailing_stop_price NUMERIC(20, 8),

    highest_price NUMERIC(20, 8),
    lowest_price NUMERIC(20, 8),

    initial_max_loss NUMERIC(20, 8) NOT NULL,

    realized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,
    unrealized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,

    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,

    close_reason TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE trade_intents
ADD COLUMN position_id UUID
REFERENCES positions(id);

CREATE TABLE position_legs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    position_id UUID NOT NULL
        REFERENCES positions(id)
        ON DELETE CASCADE,

    contract_symbol TEXT NOT NULL,

    side TEXT NOT NULL,

    quantity NUMERIC(20, 8) NOT NULL,

    multiplier INTEGER NOT NULL DEFAULT 100,

    average_entry_price NUMERIC(20, 8),
    current_price NUMERIC(20, 8),

    realized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,
    unrealized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,

    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,

    UNIQUE (
        position_id,
        contract_symbol,
        side
    )
);

CREATE TABLE position_snapshots (
    id BIGSERIAL PRIMARY KEY,

    position_id UUID NOT NULL
        REFERENCES positions(id)
        ON DELETE CASCADE,

    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    current_price NUMERIC(20, 8),
    market_value NUMERIC(24, 8),

    unrealized_pnl NUMERIC(20, 8),
    unrealized_pnl_pct NUMERIC(10, 6),

    stop_loss_price NUMERIC(20, 8),
    take_profit_price NUMERIC(20, 8),
    trailing_stop_price NUMERIC(20, 8),

    dte INTEGER,

    greeks JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);


-- =========================================================
-- COOLDOWNS
-- =========================================================

CREATE TABLE cooldowns (
    symbol TEXT NOT NULL
        REFERENCES instruments(symbol),

    strategy TEXT NOT NULL
        CHECK (
            strategy IN (
                'core',
                'active',
                'intraday'
            )
        ),

    direction TEXT NOT NULL
        CHECK (
            direction IN (
                'long',
                'short'
            )
        ),

    reason TEXT NOT NULL,

    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,

    source_position_id UUID
        REFERENCES positions(id),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (
        symbol,
        strategy,
        direction
    ),

    CHECK (ends_at > starts_at)
);


-- =========================================================
-- PORTFOLIO AND RISK
-- =========================================================

CREATE TABLE portfolio_snapshots (
    id BIGSERIAL PRIMARY KEY,

    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    equity NUMERIC(24, 8) NOT NULL,
    cash NUMERIC(24, 8) NOT NULL,
    buying_power NUMERIC(24, 8),

    stock_market_value NUMERIC(24, 8) NOT NULL DEFAULT 0,
    options_market_value NUMERIC(24, 8) NOT NULL DEFAULT 0,

    stock_open_risk NUMERIC(24, 8) NOT NULL DEFAULT 0,
    options_open_risk NUMERIC(24, 8) NOT NULL DEFAULT 0,

    daily_pnl NUMERIC(24, 8) NOT NULL DEFAULT 0,
    daily_pnl_pct NUMERIC(10, 6) NOT NULL DEFAULT 0,

    open_stock_positions INTEGER NOT NULL DEFAULT 0,
    open_options_positions INTEGER NOT NULL DEFAULT 0,

    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE risk_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    severity TEXT NOT NULL
        CHECK (
            severity IN (
                'info',
                'warning',
                'critical'
            )
        ),

    event_code TEXT NOT NULL,

    symbol TEXT
        REFERENCES instruments(symbol),

    position_id UUID
        REFERENCES positions(id),

    trade_intent_id UUID
        REFERENCES trade_intents(id),

    message TEXT NOT NULL,

    details JSONB NOT NULL DEFAULT '{}'::jsonb,

    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);


-- =========================================================
-- BOT CONTROL AND KILL SWITCH
-- =========================================================

CREATE TABLE bot_control (
    id SMALLINT PRIMARY KEY DEFAULT 1
        CHECK (id = 1),

    trading_mode TEXT NOT NULL DEFAULT 'paper'
        CHECK (trading_mode IN ('paper', 'live')),

    execution_enabled BOOLEAN NOT NULL DEFAULT false,
    new_entries_enabled BOOLEAN NOT NULL DEFAULT false,

    kill_switch_active BOOLEAN NOT NULL DEFAULT false,
    kill_switch_reason TEXT,

    last_heartbeat_at TIMESTAMPTZ,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO bot_control (id)
VALUES (1);


-- =========================================================
-- WORK QUEUE AND JOB MONITORING
-- =========================================================

CREATE TABLE work_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    task_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,

    payload JSONB NOT NULL,

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'running',
                'completed',
                'failed',
                'dead'
            )
        ),

    priority INTEGER NOT NULL DEFAULT 100,

    run_after TIMESTAMPTZ NOT NULL DEFAULT now(),

    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,

    leased_by TEXT,
    leased_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,

    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE job_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    job_name TEXT NOT NULL,

    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,

    status TEXT NOT NULL DEFAULT 'running'
        CHECK (
            status IN (
                'running',
                'completed',
                'partial',
                'failed',
                'skipped'
            )
        ),

    records_read INTEGER NOT NULL DEFAULT 0,
    records_written INTEGER NOT NULL DEFAULT 0,

    error_message TEXT,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);


-- =========================================================
-- AUDIT LOG
-- =========================================================

CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,

    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    actor_type TEXT NOT NULL,
    actor_id TEXT,

    action TEXT NOT NULL,

    entity_type TEXT NOT NULL,
    entity_id TEXT,

    correlation_id UUID,

    details JSONB NOT NULL DEFAULT '{}'::jsonb
);


-- =========================================================
-- INDEXES
-- =========================================================

CREATE INDEX idx_instruments_enabled
ON instruments (
    is_trade_candidate,
    stock_enabled
);

CREATE INDEX idx_earnings_report_date
ON earnings_events (
    report_date,
    symbol
);

CREATE INDEX idx_source_events_processing
ON source_events (
    processing_status,
    published_at
);

CREATE INDEX idx_source_event_symbols_symbol
ON source_event_symbols (
    symbol,
    source_event_id
);

CREATE INDEX idx_ai_analyses_status
ON ai_analyses (
    status,
    requested_at
);

CREATE INDEX idx_ai_analyses_symbol
ON ai_analyses (
    symbol,
    completed_at DESC
);

CREATE INDEX idx_market_snapshots_symbol_time
ON market_snapshots (
    symbol,
    captured_at DESC
);

CREATE INDEX idx_trade_theses_pending
ON trade_theses (
    status,
    confirmation_due_at,
    expires_at
);

CREATE INDEX idx_trade_intents_status
ON trade_intents (
    status,
    expires_at
);

CREATE INDEX idx_broker_orders_status
ON broker_orders (
    status,
    last_synced_at
);

CREATE INDEX idx_positions_open
ON positions (
    status,
    symbol
);

CREATE INDEX idx_position_snapshots_position_time
ON position_snapshots (
    position_id,
    captured_at DESC
);

CREATE INDEX idx_portfolio_snapshots_time
ON portfolio_snapshots (
    captured_at DESC
);

CREATE INDEX idx_risk_events_time
ON risk_events (
    severity,
    occurred_at DESC
);

CREATE INDEX idx_work_queue_ready
ON work_queue (
    status,
    run_after,
    priority
);

CREATE INDEX idx_job_runs_name_time
ON job_runs (
    job_name,
    started_at DESC
);

CREATE INDEX idx_audit_log_time
ON audit_log (
    occurred_at DESC
);


-- =========================================================
-- UPDATED_AT TRIGGERS
-- =========================================================

CREATE TRIGGER trg_instruments_updated_at
BEFORE UPDATE ON instruments
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_earnings_events_updated_at
BEFORE UPDATE ON earnings_events
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_event_clusters_updated_at
BEFORE UPDATE ON event_clusters
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_trade_theses_updated_at
BEFORE UPDATE ON trade_theses
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_trade_intents_updated_at
BEFORE UPDATE ON trade_intents
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_broker_orders_updated_at
BEFORE UPDATE ON broker_orders
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_positions_updated_at
BEFORE UPDATE ON positions
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_bot_control_updated_at
BEFORE UPDATE ON bot_control
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_work_queue_updated_at
BEFORE UPDATE ON work_queue
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();


-- =========================================================
-- REGISTER MIGRATION
-- =========================================================

INSERT INTO schema_migrations (version)
VALUES ('001_initial_schema');

COMMIT;
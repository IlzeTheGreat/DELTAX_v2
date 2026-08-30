-- File: db/migrations/002_seed_strategy_and_universes.sql
-- Purpose: Add dynamic trading universes and seed DELTAX v2 strategy v1.
-- Safe to run more than once. Requires 001_initial_schema.sql.

BEGIN;


-- =========================================================
-- DYNAMIC TRADING UNIVERSES
-- =========================================================

CREATE TABLE IF NOT EXISTS universes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,

    asset_class TEXT NOT NULL DEFAULT 'stock'
        CHECK (asset_class IN ('stock', 'crypto')),

    universe_type TEXT NOT NULL
        CHECK (universe_type IN ('base', 'candidate', 'news')),

    is_dynamic BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS universe_memberships (
    universe_id UUID NOT NULL
        REFERENCES universes(id)
        ON DELETE CASCADE,

    symbol TEXT NOT NULL
        REFERENCES instruments(symbol)
        ON DELETE CASCADE,

    is_enabled BOOLEAN NOT NULL DEFAULT true,
    rank INTEGER CHECK (rank IS NULL OR rank > 0),

    source TEXT NOT NULL DEFAULT 'seed',

    eligible_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    eligible_until TIMESTAMPTZ,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (universe_id, symbol),

    CHECK (
        eligible_until IS NULL
        OR eligible_until > eligible_from
    )
);

CREATE INDEX IF NOT EXISTS idx_universe_memberships_symbol
ON universe_memberships (symbol, is_enabled);

CREATE INDEX IF NOT EXISTS idx_universe_memberships_eligible
ON universe_memberships (universe_id, is_enabled, eligible_until);

DROP TRIGGER IF EXISTS trg_universes_updated_at ON universes;

CREATE TRIGGER trg_universes_updated_at
BEFORE UPDATE ON universes
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_universe_memberships_updated_at
ON universe_memberships;

CREATE TRIGGER trg_universe_memberships_updated_at
BEFORE UPDATE ON universe_memberships
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();


-- =========================================================
-- MARKET PROXIES
-- =========================================================

INSERT INTO instruments (
    symbol,
    alpaca_symbol,
    company_name,
    asset_type,
    is_trade_candidate,
    is_market_proxy,
    stock_enabled,
    options_enabled,
    metadata
)
VALUES
    (
        'SPY', 'SPY', 'SPDR S&P 500 ETF Trust', 'etf',
        false, true, true, false,
        '{"proxy_type":"market","index":"S&P 500"}'::jsonb
    ),
    (
        'QQQ', 'QQQ', 'Invesco QQQ Trust', 'etf',
        false, true, true, false,
        '{"proxy_type":"market","index":"Nasdaq-100"}'::jsonb
    ),
    (
        'IWM', 'IWM', 'iShares Russell 2000 ETF', 'etf',
        false, true, true, false,
        '{"proxy_type":"market","index":"Russell 2000"}'::jsonb
    )
ON CONFLICT (symbol) DO UPDATE
SET
    alpaca_symbol = EXCLUDED.alpaca_symbol,
    company_name = COALESCE(
        instruments.company_name,
        EXCLUDED.company_name
    ),
    asset_type = EXCLUDED.asset_type,
    is_trade_candidate = false,
    is_market_proxy = true,
    stock_enabled = true,
    metadata = instruments.metadata || EXCLUDED.metadata;


-- =========================================================
-- ALYRISE BASE STOCK UNIVERSE (119 SYMBOLS)
-- =========================================================

WITH seed_symbols(symbol) AS (
    VALUES
        ('AAPL'),
        ('ABT'),
        ('ABBV'),
        ('ADBE'),
        ('ADI'),
        ('AMAT'),
        ('AMD'),
        ('AMGN'),
        ('AMZN'),
        ('AVGO'),
        ('BA'),
        ('BAC'),
        ('BKNG'),
        ('BLK'),
        ('BRK.B'),
        ('CAT'),
        ('CDNS'),
        ('CI'),
        ('COP'),
        ('COST'),
        ('CRM'),
        ('CSCO'),
        ('CVX'),
        ('DE'),
        ('DHR'),
        ('DIS'),
        ('ELV'),
        ('GE'),
        ('GOOG'),
        ('GOOGL'),
        ('GS'),
        ('HD'),
        ('HON'),
        ('IBM'),
        ('INTC'),
        ('ISRG'),
        ('JNJ'),
        ('JPM'),
        ('KO'),
        ('LIN'),
        ('LLY'),
        ('LOW'),
        ('LRCX'),
        ('MA'),
        ('MCD'),
        ('MDT'),
        ('META'),
        ('MRK'),
        ('MS'),
        ('MSFT'),
        ('MU'),
        ('NEE'),
        ('NFLX'),
        ('NOW'),
        ('NVDA'),
        ('ORCL'),
        ('PANW'),
        ('PEP'),
        ('PG'),
        ('PLD'),
        ('PM'),
        ('QCOM'),
        ('REGN'),
        ('RTX'),
        ('SCHW'),
        ('SNPS'),
        ('SPGI'),
        ('TGT'),
        ('TMO'),
        ('TSLA'),
        ('TXN'),
        ('UBER'),
        ('UNH'),
        ('V'),
        ('VRTX'),
        ('VZ'),
        ('WMT'),
        ('XOM'),
        ('IREN'),
        ('EOG'),
        ('OXY'),
        ('DVN'),
        ('FANG'),
        ('EQT'),
        ('SLB'),
        ('HAL'),
        ('MPC'),
        ('VLO'),
        ('PSX'),
        ('KMI'),
        ('WMB'),
        ('OKE'),
        ('CEG'),
        ('ENPH'),
        ('RKLB'),
        ('DUK'),
        ('SO'),
        ('D'),
        ('AEP'),
        ('VST'),
        ('EXC'),
        ('ETN'),
        ('VRT'),
        ('PWR'),
        ('EMR'),
        ('ROK'),
        ('JCI'),
        ('HUBB'),
        ('CARR'),
        ('LMT'),
        ('NOC'),
        ('GD'),
        ('LHX'),
        ('HWM'),
        ('TDG'),
        ('CW'),
        ('PEG'),
        ('XEL'),
        ('PCG')
)
INSERT INTO instruments (
    symbol,
    alpaca_symbol,
    asset_type,
    is_trade_candidate,
    is_market_proxy,
    stock_enabled,
    metadata
)
SELECT
    symbol,
    symbol,
    'stock',
    true,
    false,
    true,
    jsonb_build_object('seed_universe', 'alyrise_base')
FROM seed_symbols
ON CONFLICT (symbol) DO UPDATE
SET
    alpaca_symbol = EXCLUDED.alpaca_symbol,
    asset_type = 'stock',
    is_trade_candidate = true,
    stock_enabled = true,
    metadata = instruments.metadata || EXCLUDED.metadata;


-- =========================================================
-- UNIVERSE DEFINITIONS
-- =========================================================

INSERT INTO universes (
    code,
    name,
    description,
    asset_class,
    universe_type,
    is_dynamic,
    is_active,
    metadata
)
VALUES
    (
        'alyrise_base',
        'Alyrise Base Universe',
        'Stable source universe containing the 119 approved stocks.',
        'stock',
        'base',
        false,
        true,
        '{"membership_owner":"migration","expected_members":119}'::jsonb
    ),
    (
        'core_candidates',
        'Core Candidates',
        'Dynamic candidates selected for the Core strategy.',
        'stock',
        'candidate',
        true,
        true,
        '{"strategy":"core","max_per_scan":3}'::jsonb
    ),
    (
        'active_candidates',
        'Active Candidates',
        'Dynamic candidates selected for the Active strategy.',
        'stock',
        'candidate',
        true,
        true,
        '{"strategy":"active","max_per_scan":3}'::jsonb
    ),
    (
        'intraday_candidates',
        'Intraday Candidates',
        'Dynamic candidates selected for the Intraday strategy.',
        'stock',
        'candidate',
        true,
        true,
        '{"strategy":"intraday","max_per_scan":5}'::jsonb
    ),
    (
        'news_candidates',
        'News Candidates',
        'Dynamic symbols with relevant recent news or earnings events.',
        'stock',
        'news',
        true,
        true,
        '{"event_driven":true}'::jsonb
    )
ON CONFLICT (code) DO UPDATE
SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    asset_class = EXCLUDED.asset_class,
    universe_type = EXCLUDED.universe_type,
    is_dynamic = EXCLUDED.is_dynamic,
    is_active = EXCLUDED.is_active,
    metadata = universes.metadata || EXCLUDED.metadata;


-- Only the stable base universe is populated by this migration.
-- Candidate/news memberships are written dynamically by scanner jobs.

INSERT INTO universe_memberships (
    universe_id,
    symbol,
    is_enabled,
    source,
    metadata
)
SELECT
    u.id,
    i.symbol,
    true,
    '002_seed_strategy_and_universes',
    '{"seeded":true}'::jsonb
FROM universes u
JOIN instruments i
    ON i.metadata->>'seed_universe' = 'alyrise_base'
WHERE u.code = 'alyrise_base'
ON CONFLICT (universe_id, symbol) DO UPDATE
SET
    is_enabled = true,
    source = EXCLUDED.source,
    eligible_until = NULL,
    metadata = universe_memberships.metadata || EXCLUDED.metadata;


-- =========================================================
-- DELTAX V2 STRATEGY CONFIGURATION
-- =========================================================

-- Deactivate any previous configuration before activating v1, because
-- 001_initial_schema enforces that only one configuration can be active.
UPDATE strategy_configs
SET is_active = false
WHERE is_active = true
  AND version <> 'deltax_v2_strategy_v1';

INSERT INTO strategy_configs (
    version,
    name,
    config,
    is_active,
    activated_at
)
VALUES (
    'deltax_v2_strategy_v1',
    'DELTAX v2 Stock and Defined-Risk Options Strategy v1',
    $config$
    {
      "schema_version": 1,
      "environment": "paper",
      "account_reference_equity_usd": 100000,
      "scanner": {
        "interval_minutes": 5,
        "price_confirmation_minutes": 10,
        "new_entries_cutoff_minutes_before_close": 30
      },
      "universes": {
        "base": "alyrise_base",
        "core_candidates": "core_candidates",
        "active_candidates": "active_candidates",
        "intraday_candidates": "intraday_candidates",
        "news_candidates": "news_candidates"
      },
      "capital": {
        "stocks_max_usd": 70000,
        "options_max_usd": 30000,
        "crypto_max_usd": 0
      },
      "portfolio_risk": {
        "max_stock_loss_per_trade_usd": 1000,
        "max_option_loss_per_trade_usd": 1000,
        "max_combined_loss_per_underlying_usd": 2000,
        "pause_new_entries_daily_loss_pct": 0.03,
        "kill_switch_daily_loss_pct": 0.05,
        "fail_closed_on_missing_required_state": true
      },
      "market_regime": {
        "proxy_symbols": ["SPY", "QQQ", "IWM"],
        "weak_when_below_intraday_vwap": true,
        "long_entry_drop_pct_by_weak_proxy_count": {
          "core": {
            "0": 0.045,
            "1": 0.050,
            "2": 0.055,
            "3": 0.060,
            "missing": 0.060
          },
          "active": {
            "0": 0.045,
            "1": 0.050,
            "2": 0.060,
            "3": 0.070,
            "missing": 0.070
          },
          "intraday": {
            "0": 0.025,
            "1": 0.030,
            "2": 0.035,
            "3": 0.050,
            "missing": 0.035
          }
        }
      },
      "stock_strategies": {
        "core": {
          "enabled": true,
          "reference": "average_daily_vwap",
          "reference_lookback_days": 7,
          "max_candidates_per_scan": 3,
          "base_long_drop_pct": 0.045,
          "base_short_rise_pct": 0.045,
          "options_allowed": true,
          "trailing_activation_gain_pct": 0.08,
          "trailing_distance_pct": 0.02,
          "stop_loss_cooldown_minutes": 10080
        },
        "active": {
          "enabled": true,
          "reference": "average_daily_vwap",
          "reference_lookback_days": 20,
          "max_candidates_per_scan": 3,
          "base_long_drop_pct": 0.045,
          "base_short_rise_pct": 0.045,
          "options_allowed": true,
          "trailing_activation_gain_pct": 0.05,
          "trailing_distance_pct": 0.02,
          "stop_loss_cooldown_minutes": 10080
        },
        "intraday": {
          "enabled": true,
          "reference": "intraday_vwap",
          "long_requires_below_previous_close": true,
          "short_requires_above_previous_close": true,
          "max_candidates_per_scan": 5,
          "base_long_drop_pct": 0.025,
          "base_short_rise_pct": 0.025,
          "options_allowed": false,
          "trailing_activation_gain_pct": 0.012,
          "trailing_distance_pct": 0.01,
          "max_holding_hours": 24,
          "timed_exit_min_profit_pct": 0.0007,
          "stop_loss_cooldown_minutes": 90
        }
      },
      "stock_position_management": {
        "atr_period": 14,
        "stop_loss_atr_multiple": 1.5,
        "take_profit_atr_multiple": 2.0,
        "normal_exit_cooldown_minutes": 180,
        "position_size_formula": "min(1000 / stop_distance, available_strategy_capital / latest_price)"
      },
      "short_rules": {
        "enabled": true,
        "requires_atr_confirmation": true,
        "requires_bearish_ai_catalyst": true,
        "requires_price_reversal_confirmation": true,
        "confirmation_minutes": 10,
        "requires_alpaca_shortable": true,
        "requires_alpaca_easy_to_borrow": true,
        "requires_market_and_sector_alignment": true,
        "thresholds_may_only_be_tightened": true
      },
      "ai_gate": {
        "required": true,
        "allowed_directions": ["bullish", "bearish", "neutral"],
        "required_outputs": [
          "direction",
          "confidence",
          "time_horizon",
          "catalyst",
          "risks",
          "invalidation_condition"
        ],
        "may_reject_trade": true,
        "may_change_deterministic_risk_limits": false
      },
      "options": {
        "enabled": true,
        "strategy_name": "AI-Directed Defined-Risk Premium Strategy",
        "structure": "directional_credit_spread",
        "allowed_stock_strategies": ["core", "active"],
        "naked_short_options_allowed": false,
        "iron_condor_allowed": false,
        "order_class": "mleg",
        "order_type": "limit",
        "market_orders_allowed": false,
        "min_dte": 7,
        "max_dte": 21,
        "force_exit_dte": 3,
        "short_leg_abs_delta_min": 0.20,
        "short_leg_abs_delta_max": 0.30,
        "min_credit_to_spread_width_ratio": 0.30,
        "max_loss_per_position_usd": 1000,
        "max_open_positions": 5,
        "max_total_theoretical_loss_usd": 5000,
        "max_positions_per_underlying": 1,
        "max_combined_ideas_per_sector": 2,
        "requires_bid_and_ask_for_all_legs": true,
        "requires_liquidity_check": true,
        "requires_open_interest_check": true,
        "requires_implied_volatility_check": true,
        "requires_complete_risk_data": true,
        "earnings_blackout_full_trading_days": 1,
        "hold_through_earnings": false,
        "profit_target_credit_capture_pct": 0.50,
        "stop_loss_credit_multiple": 2.0,
        "close_on_ai_invalidation": true,
        "close_on_danger_boundary": true
      }
    }
    $config$::jsonb,
    true,
    now()
)
ON CONFLICT (version) DO UPDATE
SET
    name = EXCLUDED.name,
    config = EXCLUDED.config,
    is_active = true,
    activated_at = COALESCE(
        strategy_configs.activated_at,
        EXCLUDED.activated_at
    );


-- =========================================================
-- REGISTER MIGRATION
-- =========================================================

INSERT INTO schema_migrations (version)
VALUES ('002_seed_strategy_and_universes')
ON CONFLICT (version) DO NOTHING;

COMMIT;

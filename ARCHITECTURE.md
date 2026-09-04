# DELTAX V2 Architecture

This document describes the architecture of **DELTAX V2**, an autonomous AI-assisted stock and options trading system built for the **Alpaca AI Trading Agents Hackathon 2026**.

The core design principle is simple:

> Technical analysis finds a possible opportunity.  
> AI interprets the real-world information around it.  
> Deterministic risk controls decide whether the trade is allowed.  
> Alpaca executes the approved decision.  
> Neon stores the evidence.

---

## 1. System Overview

```text
                    ┌──────────────────────┐
                    │      DATA INPUTS     │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
        Alpaca News         Finnhub         Marketaux
              │                │                │
              └────────────────┴────────────────┘
                               │
                               ▼
                    NEWS INGESTION LAYER
                               │
                               ▼
                     EVENT CLUSTERING
                               │
                               ▼
                    AI MARKET ANALYSIS
                               │
                  ┌────────────┴────────────┐
                  │                         │
                  ▼                         ▼
          COMPANY / SYMBOL AI        MARKET REGIME AI
                  │                         │
                  └────────────┬────────────┘
                               ▼
                    TECHNICAL SIGNAL ENGINE
                               │
                               ▼
                      DIRECTION ROUTER
                               │
                               ▼
                    DETERMINISTIC RISK GATES
                               │
                               ▼
                        TRADE INTENT
                               │
                               ▼
                    ALPACA PAPER EXECUTION
                               │
                               ▼
                  ORDER / FILL RECONCILIATION
                               │
                               ▼
                     POSITION MONITORING
                               │
                               ▼
                        EXIT ENGINE
                               │
                               ▼
                      NEON POSTGRESQL
                               │
                               ▼
                    STREAMLIT JURY DASHBOARD
```

---

## 2. Architectural Principles

DELTAX V2 separates trading into independent decision layers rather than allowing one model or one indicator to directly submit orders.

### Layer 1 — Market Observation
The system continuously collects:

- market prices;
- technical indicators;
- market news;
- company news;
- market and sector context;
- broker account state.

### Layer 2 — AI Interpretation
AI is used for interpretation, not unrestricted execution.

The AI determines:

- whether a catalyst is material;
- likely bullish or bearish impact;
- confidence;
- affected sectors;
- affected indexes;
- affected symbols;
- expected time horizon;
- supporting evidence;
- important risks.

### Layer 3 — Deterministic Risk
The AI cannot bypass the deterministic risk engine.

Even a high-confidence AI signal can be rejected due to:

- portfolio limits;
- conflicting market state;
- sector risk;
- missing confirmation;
- cooldown;
- existing position;
- market timing;
- drawdown protection;
- kill switch.

### Layer 4 — Broker Execution
Only approved trade intents reach Alpaca.

### Layer 5 — Reconciliation & Audit
Broker state is reconciled back into the internal system and persisted in Neon.

---

## 3. News & AI Pipeline

### Sources

DELTAX V2 ingests news from:

- **Alpaca News**
- **Finnhub**
- **Marketaux**

### Processing Flow

```text
Raw Articles
    ↓
Normalize / Deduplicate
    ↓
Store as source_events
    ↓
Cluster related stories
    ↓
Create event_clusters
    ↓
AI analysis
    ↓
Persist structured market impact
```

### Why Clustering Exists

Multiple publishers can report the same catalyst.

Without clustering, one event could appear to the model as several independent bullish or bearish signals.

Clustering reduces duplication and creates a more coherent market event representation before AI analysis.

### Main Components

Examples of relevant modules:

```text
deltax/market_news_ingestion.py
deltax/company_news_ingestion.py
deltax/market_event_clustering.py
deltax/market_impact_ai.py
deltax/market_news_worker.py
deltax/company_news_worker.py
```

---

## 4. Technical Signal Layer

The technical engine identifies possible setups before AI and risk gating.

The system supports:

- **Core**
- **Active**
- **Intraday**

Signals can use information such as:

- price movement;
- VWAP;
- deviation from VWAP;
- ATR / volatility;
- index context;
- sector context;
- intraday state;
- confirmation price.

The technical layer answers:

> Is there a statistically / structurally interesting setup?

It does **not** answer:

> Should we execute it right now?

That decision belongs to the later AI + risk layers.

---

## 5. Direction Router

The direction router combines technical and AI information into a final directional interpretation.

Relevant module:

```text
deltax/direction_router.py
```

Conceptually:

```text
Technical LONG
    +
Fresh Bearish Material News
    ↓
BLOCK / REJECT

Technical SHORT
    +
Fresh Bullish Material News
    ↓
BLOCK / REJECT

Technical LONG
    +
Bullish AI Context
    +
Confirmation
    ↓
LONG CANDIDATE
```

---

## 6. Strategy-Specific Logic

## Core / Active

Core and Active positions use slower confirmation logic when material news is involved.

```text
Technical Setup
    ↓
AI News Context
    ↓
10-Minute Confirmation
    ↓
Risk Gate
    ↓
Execution
```

## Intraday

Intraday is intentionally faster.

```text
Technical Setup
    ↓
Immediate Fresh-News Check
    ↓
Risk Gate
    ↓
Execution
```

Rules:

- bearish fresh news can block LONG;
- bullish fresh news can block SHORT;
- no 10-minute waiting period.

---

## 7. Risk Engine

Risk controls are deterministic and separate from AI.

### Typical Risk Gates

- AI confidence threshold;
- materiality;
- conflicting fresh news;
- market-state gate;
- sector-state gate;
- technical confirmation;
- cooldown state;
- strategy timing;
- existing-position check;
- duplicate-order protection;
- account state;
- broker state;
- portfolio risk.

### Per-Trade Risk

Hackathon paper account:

```text
Starting equity: $100,000
Target maximum planned risk per trade: 1%
Maximum planned loss: $1,000
```

### Daily Protection

```text
-3% daily drawdown
    ↓
Disable new entries

-5% daily drawdown
    ↓
Kill switch
```

### Fail-Closed Design

When execution-critical data is unavailable or inconsistent, the system is intended to block the trade rather than guess.

---

## 8. Stock Execution Architecture

```text
Technical Signal
    ↓
Trade Thesis
    ↓
AI Context
    ↓
Risk Gate
    ↓
Approved Trade Intent
    ↓
Paper Executor
    ↓
Alpaca Order
    ↓
Broker Reconciliation
    ↓
Internal Position
```

Relevant modules include:

```text
deltax/agent_cycle.py
deltax/trading_cycle.py
deltax/decision_persistence.py
deltax/broker_order_reconciler.py
deltax/portfolio_risk_monitor.py
deltax/exit_intent_builder.py
```

---

## 9. Options Architecture

Options are implemented as **defined-risk multi-leg credit spreads**.

### Bullish

```text
Bull Put Credit Spread
```

### Bearish

```text
Bear Call Credit Spread
```

### Contract Rules

```text
DTE: 7–21 days
Short-leg |delta|: approx. 0.20–0.30
Minimum credit: approx. 30% of spread width
Maximum planned loss: $1,000
```

### Options Flow

```text
Directional Thesis
    ↓
Options Eligibility
    ↓
Contract Discovery
    ↓
Spread Construction
    ↓
Risk Validation
    ↓
Multi-Leg Trade Intent
    ↓
Alpaca MLEG Order
    ↓
Reconciliation
```

Relevant module:

```text
deltax/options_spread_intent_builder.py
```

---

## 10. Broker Execution

Alpaca is the execution source of truth.

The broker layer handles:

- account state;
- market clock;
- stock orders;
- short orders;
- options contracts;
- multi-leg options orders;
- fills;
- order status;
- positions;
- reconciliation.

### Important Design Rule

The database does not assume an order filled just because the system submitted it.

Instead:

```text
Submit Order
    ↓
Receive Alpaca Order ID
    ↓
Persist Broker Order
    ↓
Poll / Reconcile Broker State
    ↓
Persist Fill / Position
```

---

## 11. Reconciliation Layer

Reconciliation protects against drift between:

- internal trade intent;
- internal position state;
- Alpaca broker state.

Relevant module:

```text
deltax/broker_order_reconciler.py
```

The broker is treated as authoritative for:

- fill status;
- filled quantity;
- average fill price;
- open quantity;
- order state.

---

## 12. Position & Exit Management

Once positions are open, DELTAX continues monitoring them.

Possible exit causes include:

- take profit;
- stop loss;
- AI direction flip;
- technical invalidation;
- portfolio risk;
- strategy-specific end-of-day rules;
- kill switch.

Exit intents are created through the same controlled architecture rather than allowing arbitrary broker actions.

Relevant module:

```text
deltax/exit_intent_builder.py
```

---

## 13. Persistence & Audit Trail

Neon PostgreSQL stores the decision history.

The system persists objects such as:

- source news events;
- event clusters;
- AI analyses;
- trade theses;
- trade intents;
- options legs;
- broker orders;
- fills;
- trades;
- positions;
- position snapshots;
- risk events;
- portfolio snapshots;
- cooldown state;
- control state.

This allows the jury to trace:

```text
Why was this trade considered?
        ↓
What did AI say?
        ↓
Which risk gates passed?
        ↓
What order was created?
        ↓
What did Alpaca fill?
        ↓
What happened to the position?
        ↓
Why was it exited?
```

---

## 14. Dashboard Architecture

The dashboard is deliberately **read-only**.

It does not submit, edit, or cancel orders.

It reads persisted state from Neon and presents:

- account equity;
- daily P&L;
- contest P&L;
- weekly portfolio equity curve;
- week high / low;
- NYSE / ET timing;
- open stock risk;
- open options risk;
- open positions;
- Decision Feed;
- technical reasoning;
- AI reasoning;
- trade execution state;
- rejected theses;
- risk events;
- system heartbeat.

Main dashboard:

```text
dashboard/app.py
```

---

## 15. Scheduling Architecture

Long-running work is split into independent processes.

### Market News Worker

Handles:

```text
Alpaca + Finnhub + Marketaux
    ↓
Market Event Clustering
    ↓
Market Impact AI
```

### Company News Worker

Handles company-specific ingestion and AI processing.

### Trading Cycle

Handles stock/options execution logic.

### ETF Trading Cycle

Handles the separate ETF rotation strategy.

This separation prevents slow AI/news jobs from blocking time-sensitive trading cycles.

---

## 16. Failure Isolation

The architecture separates:

- news ingestion;
- AI processing;
- signal generation;
- risk logic;
- execution;
- reconciliation;
- dashboard.

A failure in the dashboard does not stop trading.

A slow company-news AI run does not need to block the broker reconciliation process.

An execution failure is persisted and can be inspected independently.

This modular design is especially important for autonomous systems.

---

## 17. Repository Map

```text
DELTAX_v2/
│
├── dashboard/
│   ├── app.py
│   └── assets/
│
├── db/
│
├── deltax/
│   ├── agent_cycle.py
│   ├── trading_cycle.py
│   ├── direction_router.py
│   ├── market_news_ingestion.py
│   ├── company_news_ingestion.py
│   ├── market_event_clustering.py
│   ├── market_impact_ai.py
│   ├── market_news_worker.py
│   ├── company_news_worker.py
│   ├── decision_persistence.py
│   ├── options_spread_intent_builder.py
│   ├── broker_order_reconciler.py
│   ├── portfolio_risk_monitor.py
│   ├── exit_intent_builder.py
│   └── etf_trading_cycle.py
│
├── helpers/
│
├── simulation_ir/
│
├── .env.example
├── requirements.txt
└── README.md
```

---

## 18. Decision Philosophy

The architecture is intentionally not:

```text
AI says BUY
    ↓
BUY
```

Instead:

```text
Technical opportunity
    ↓
AI interpretation
    ↓
Structured thesis
    ↓
Deterministic risk
    ↓
Approved intent
    ↓
Broker execution
    ↓
Reconciliation
    ↓
Audit
```

This separation is one of the central safety and engineering principles of DELTAX V2.

---

## 19. Hackathon-Relevant Architecture Features

DELTAX V2 demonstrates:

- autonomous operation;
- AI integrated into trade decisions;
- Alpaca paper execution;
- stock and options workflows;
- multi-leg options architecture;
- deterministic risk controls;
- open-position monitoring;
- full broker reconciliation;
- persistent auditability;
- public read-only monitoring;
- modular workers and scheduled cycles.

---

## 20. Summary

DELTAX V2 is built as an auditable trading system rather than a single trading script.

The architecture separates:

```text
OBSERVE
    ↓
INTERPRET
    ↓
VALIDATE
    ↓
EXECUTE
    ↓
RECONCILE
    ↓
MONITOR
    ↓
AUDIT
```

That separation allows AI to contribute market understanding while deterministic code retains control over risk and execution.

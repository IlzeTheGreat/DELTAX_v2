# DELTAX V2

**Autonomous AI Stock & Options Trading Agent built for the Alpaca AI Trading Agents Hackathon 2026**

DELTAX V2 combines technical market signals, multi-source news intelligence, AI-based market interpretation, deterministic risk gates, Alpaca paper execution, and a full audit trail in Neon PostgreSQL.

The system is designed to answer three questions before every trade:

1. **Is there a technical opportunity?**
2. **Does current real-world information support or invalidate it?**
3. **Is the trade allowed by deterministic risk controls?**

Only after all required gates pass can DELTAX submit an order to Alpaca.

---

## Live Demo

- **Public dashboard:** https://deltax-v2.streamlit.app/
- **Public GitHub:** https://github.com/IlzeTheGreat/DELTAX_v2
- **Video demo:** `TODO: ADD VIDEO URL`
- **Hackathon Alpaca paper account ID:** PA3WE5UJCEFD

> The dashboard is read-only. It visualizes portfolio performance, trade decisions, signal reasoning, risk state, and audit data without submitting or modifying orders.

---

## What DELTAX V2 Does

DELTAX V2 is an autonomous paper-trading system that:

- scans an active stock universe for technical opportunities;
- supports **Core, Active, and Intraday** stock strategies;
- ingests market and company news from **Alpaca News, Finnhub, and Marketaux**;
- clusters related news events before AI analysis;
- uses AI to evaluate directional impact, materiality, confidence, affected sectors/symbols, evidence and risks;
- applies deterministic risk gates before execution;
- creates stock and options trade intents;
- submits stock orders and multi-leg options orders to **Alpaca Paper Trading**;
- reconciles Alpaca broker state with the internal ledger;
- monitors open positions and exits;
- persists the complete decision trail in **Neon PostgreSQL**;
- exposes performance and reasoning through a public Streamlit dashboard.

---

## High-Level Architecture

```text
ALPACA NEWS + FINNHUB + MARKETAUX
               │
               ▼
       NEWS INGESTION
               │
               ▼
        EVENT CLUSTERING
               │
               ▼
        AI MARKET ANALYSIS
               │
               ├──────────────┐
               ▼              ▼
      COMPANY CONTEXT    MARKET REGIME
               │              │
               └──────┬───────┘
                      ▼
              TECHNICAL SIGNALS
                      │
                      ▼
               DIRECTION ROUTER
                      │
                      ▼
              DETERMINISTIC
                RISK GATES
                      │
                      ▼
                TRADE INTENT
                      │
                      ▼
            ALPACA PAPER EXECUTION
                      │
                      ▼
               RECONCILIATION
                      │
                      ▼
             POSITION MONITORING
                      │
                      ▼
             NEON AUDIT TRAIL
                      │
                      ▼
             STREAMLIT DASHBOARD
```

---

## AI Decision Logic

AI is not used to blindly generate trades.

The technical engine identifies **where a potential trading setup exists**. AI evaluates whether current market and company information supports, contradicts, or invalidates that setup.

### News Intelligence

DELTAX collects news from:

- **Alpaca News**
- **Finnhub**
- **Marketaux**

Related events are clustered before analysis so multiple articles about the same catalyst are interpreted as one market event instead of independent signals.

### AI Outputs

For relevant news clusters, the AI produces structured analysis including:

- directional impact;
- confidence;
- materiality;
- source quality;
- expected time horizon;
- affected sectors;
- affected indexes;
- affected symbols;
- catalyst summary;
- supporting evidence;
- identified risks;
- invalidation conditions.

### Core / Active

For material-news setups:

- AI context is incorporated into the decision;
- a **10-minute confirmation** is required;
- price action must confirm the expected direction before execution.

### Intraday

Intraday is intentionally faster:

- there is no 10-minute waiting period;
- a qualifying technical setup can act immediately;
- fresh bearish news blocks LONG trades;
- fresh bullish news blocks SHORT trades.

### Confidence Gate

```text
AI confidence >= 0.65
```

---

## Stock Trading Logic

DELTAX supports both **LONG** and **SHORT** positions.

The stock engine combines:

- price behaviour;
- VWAP deviation;
- ATR / volatility context;
- broader market state;
- sector state;
- news context;
- AI confidence;
- confirmation logic;
- existing position state;
- cooldown state;
- portfolio risk.

A technical opportunity can therefore be rejected even when the price setup is valid if the real-world information or risk context does not support the trade.

---

## Options Trading

Options are a first-class part of DELTAX V2.

The agent uses **defined-risk credit spreads** rather than naked options exposure.

### Bullish Thesis

```text
Bull Put Credit Spread
```

### Bearish Thesis

```text
Bear Call Credit Spread
```

### Contract Selection

Current options rules include:

- **7–21 DTE**
- short-leg absolute delta approximately **0.20–0.30**
- minimum credit approximately **30% of spread width**
- liquidity checks
- predefined maximum loss
- one multi-leg order routed through Alpaca

### Risk

```text
Maximum planned loss per options position = $1,000
```

Multi-leg spreads are submitted through Alpaca as a single **MLEG** order.

---

## Risk Management

Every trade must pass deterministic controls before execution.

Risk gates include:

- AI confidence threshold;
- fresh or conflicting news;
- market state;
- sector state;
- technical confirmation;
- cooldown restrictions;
- existing-position checks;
- strategy-specific timing rules;
- account state;
- broker state;
- market-open state;
- duplicate-order prevention;
- portfolio risk limits.

### Per-Trade Risk

```text
Maximum planned risk per trade = 1% of $100,000 = $1,000
```

### Daily Drawdown Controls

```text
-3% daily drawdown  → disable new entries
-5% daily drawdown  → activate kill switch
```

The system is designed to fail closed when critical information required for execution is missing or inconsistent.

---

## Alpaca Integration

DELTAX uses Alpaca for:

- paper trading;
- account and market-clock state;
- stock execution;
- short execution;
- options contracts;
- multi-leg options orders;
- order status;
- fills;
- open positions;
- reconciliation;
- market/news data.

The executor verifies execution safety before submission, including:

- paper-trading mode;
- execution enabled;
- account not blocked;
- market open;
- kill switch not active;
- valid non-expired intent;
- no duplicate broker order.

Broker state is treated as the final execution source of truth.

### Alpaca MCP / CLI

`TODO: DESCRIBE EXACTLY HOW ALPACA MCP OR CLI WAS USED IN THE PROJECT.`

> This section must be completed with the real implementation. Do not claim MCP/CLI usage unless it was actually used.

---

## Execution & Reconciliation

```text
Signal
  ↓
Trade Thesis
  ↓
Risk Gate
  ↓
Approved Trade Intent
  ↓
Alpaca Paper Order
  ↓
Broker Order Record
  ↓
Fill / Order Reconciliation
  ↓
Internal Position State
  ↓
Portfolio Snapshot
```

Orders use deterministic client order IDs to improve idempotency and auditability.

---

## Auditability

DELTAX is designed so the jury can inspect **why a trade happened**, not only whether it made or lost money.

Neon PostgreSQL stores data such as:

- source news events;
- event clusters;
- cluster members;
- AI analyses;
- technical trade theses;
- trade intents;
- option legs;
- broker orders;
- broker order events;
- fills;
- trades;
- positions;
- position snapshots;
- risk events;
- portfolio snapshots;
- cooldown state;
- strategy state.

A trade can therefore be followed through:

```text
Market / News Event
        ↓
Technical Signal
        ↓
AI Reasoning
        ↓
Risk Gates
        ↓
Trade Intent
        ↓
Alpaca Order
        ↓
Fill / Position
        ↓
Exit
```

---

## Public Jury Dashboard

The Streamlit dashboard provides a read-only view of the agent.

It includes:

- current paper account equity;
- current-day P&L;
- P&L versus the $100,000 contest starting balance;
- contest-week equity curve;
- week high / week low;
- NYSE / ET market-session timing;
- open stock and options risk;
- open positions;
- Decision Feed;
- technical reasoning;
- AI / news reasoning;
- Alpaca execution state;
- rejected trade theses;
- risk events;
- system heartbeat.

The contest-week chart uses persisted `portfolio_snapshots` from Neon.

---

## Data Sources & Tech Stack

### Market & Broker Data
- Alpaca

### News
- Alpaca News
- Finnhub
- Marketaux

### AI
- OpenAI models for structured market/news interpretation

### Persistence
- Neon PostgreSQL

### Dashboard
- Streamlit

### Core Runtime
- Python
- pandas
- psycopg
- alpaca-py
- requests

---

## Core Repository Structure

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
│   ├── broker_order_reconciler.py
│   ├── candidate_news_refresh.py
│   ├── company_news_ingestion.py
│   ├── company_news_worker.py
│   ├── decision_persistence.py
│   ├── direction_router.py
│   ├── etf_trading_cycle.py
│   ├── exit_intent_builder.py
│   ├── market_event_clustering.py
│   ├── market_impact_ai.py
│   ├── market_news_ingestion.py
│   ├── market_news_worker.py
│   ├── options_spread_intent_builder.py
│   ├── paper_executor.py
│   ├── portfolio_risk_monitor.py
│   └── trading_cycle.py
│
├── helpers/
├── simulation_ir/
├── .env.example
├── .gitignore
├── requirements.txt
├── sp500.txt
└── stocks.txt
```

---

## Setup

### 1. Clone

```bash
git clone https://github.com/IlzeTheGreat/DELTAX_v2.git
cd DELTAX_v2
```

### 2. Create Virtual Environment

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

Copy `.env.example` to `.env` and add required credentials.

Typical variables include:

```text
DATABASE_URL=
OPENAI_API_KEY=

ALPACA_API_KEY_PAPER=
ALPACA_API_SECRET_PAPER=
ALPACA_TRADING_URL_PAPER=

ALPACA_API_KEY_EVENT=
ALPACA_API_SECRET_EVENT=
ALPACA_TRADING_URL_EVENT=

FINNHUB_API_KEY=
MARKETAUX_API_KEY=
```

Never commit real secrets.

---

## Running the Agent

Examples:

```bash
python deltax/market_news_worker.py
python deltax/company_news_worker.py
python deltax/trading_cycle.py
python deltax/etf_trading_cycle.py
```

The hackathon setup uses scheduled execution rather than requiring a human to trigger every trade decision.

---

## Running the Dashboard

```bash
streamlit run dashboard/app.py
```

For Streamlit Cloud, configure `DATABASE_URL` and other required credentials through Streamlit Secrets.

---

## Why DELTAX

Many trading systems answer only:

> “Did the price signal trigger?”

DELTAX asks:

> “Did the price signal trigger, does current real-world information support it, and is the risk acceptable right now?”

The technical engine finds opportunity.  
The AI interprets causality and market context.  
The deterministic risk layer controls what is allowed.  
Alpaca executes the final approved decision.  
Neon records the evidence.  
The dashboard makes the process visible.

---

## Team

**DELTAX**

- **Ilze** — automation, infrastructure, integration, dashboard, DELTAX V2 implementation
- **Pedro** — team lead, agent development / DELTAX V1
- **Martin** — trading logic, strategy concepts, AURA

Built during the **Alpaca AI Trading Agents Hackathon 2026**.

---

## Disclaimer

DELTAX is a hackathon prototype running in an Alpaca paper-trading environment. Trading involves risk. Historical, simulated, or paper-trading performance does not guarantee future results.

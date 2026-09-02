# ⚡ REVORA — Autonomous AI Revenue Recovery Engine

> **Razorpay Buildathon · Track 03: Subscription & Recurring Payment Failures**  
> *An autonomous, agentic revenue recovery system that detects, diagnoses, and recovers involuntary churn through intelligent root-cause routing, silent network backoffs, Hinglish voice outreach, and adaptive downsell mechanics.*

---

## 🎯 The Problem vs. The Revora Solution

### ❌ The Legacy Problem: "Dumb Retries"
Traditional subscription recovery relies on static, periodic dunning schedules (e.g. retry every 24 hours 3 times). This naive approach leads to:
1. **Unnecessary Customer Churn:** Re-attempting an expired card or closed bank account triggers gateway hard-declines and frustrates customers.
2. **Gateway Penalties & Spammed Customers:** Blind WhatsApp and SMS blasts for temporary gateway timeouts ruin customer trust.
3. **Lost Revenue from High-Ticket Failures:** When a user lacks sufficient funds for a ₹10,000 charge, flat retry logic simply fails instead of offering downsell flexibility.

### 🚀 The Revora Solution: Semantic Root-Cause Routing
Revora ingests real-time Razorpay payment lifecycle webhooks with cryptographically verified HMAC-SHA256 signatures, performs semantic error classification, and executes a customized recovery workflow based on root cause, ticket size, and customer profile.

---

## 🧩 Core Autonomous Recovery Scenarios

| Failure Pattern | Diagnosed Root Cause | Autonomous Recovery Strategy | Customer Touchpoint |
|:---|:---|:---|:---|
| `GATEWAY_ERROR`, `timeout`, `bank_offline` | **Temporary Network Failure** | `SILENT_MANDATE_RETRY` | **Zero Customer Friction:** Silent exponential retry via Razorpay mandate API. |
| `expired_card`, `card_declined`, `invalid_instrument` | **Expired / Invalid Instrument** | `SECURE_PAYMENT_LINK` / `UPI_AUTOPAY_MIGRATION` | **WhatsApp & Hinglish Voice Call:** Generates instant Razorpay Link & guides user to migrate to UPI AutoPay. |
| `insufficient_funds`, `low_balance` *(₹ < 500)* | **Low-Ticket Insufficient Funds** | `SILENT_MANDATE_RETRY` | **Scheduled Retry:** Re-attempts charge after a short delay when micro-funds settle. |
| `insufficient_funds` *(₹ ≥ 500 High-Ticket)* | **High-Ticket Insufficient Funds** | `ADAPTIVE_DOWNGRADE_OFFER` | **Adaptive Downsell:** Generates instant multi-plan checkout at 50% value to retain customer without churn. |

---

## 🏛️ System Architecture

```
                       ┌──────────────────────────────────────┐
                       │     Razorpay Payment Webhooks        │
                       │   (payment.failed, mandate.failed)   │
                       └──────────────────┬───────────────────┘
                                          │ POST (HMAC-SHA256 Signed)
                                          ▼
                       ┌──────────────────────────────────────┐
                       │   Ingestion & Verification Guard     │
                       │  app/api/v1/endpoints/webhooks.py    │
                       └──────────────────┬───────────────────┘
                                          │ Persist AT_RISK Event
                                          ▼
                       ┌──────────────────────────────────────┐
                       │     Intelligent Decision Engine      │
                       │   app/services/decision_engine.py    │
                       └──────┬───────────┬────────────┬──────┘
                              │           │            │
            ┌─────────────────┘           │            └──────────────────┐
            ▼                             ▼                               ▼
 ┌──────────────────────┐    ┌─────────────────────────┐    ┌─────────────────────────┐
 │ Silent Mandate Retry │    │  Adaptive Outreach      │    │ Adaptive Downsell       │
 │ (Exponential Backoff)│    │  (WhatsApp + Hinglish)  │    │ (50% Value Checkout)    │
 └──────────┬───────────┘    └────────────┬────────────┘    └────────────┬────────────┘
            │                             │                              │
            └─────────────────────────────┼──────────────────────────────┘
                                          ▼
                       ┌──────────────────────────────────────┐
                       │    Append-Only Audit Trail Ledger    │
                       │ (InterventionAuditLog + Async SQLite)│
                       └──────────────────┬───────────────────┘
                                          ▼
                       ┌──────────────────────────────────────┐
                       │   Real-Time Operations Dashboard     │
                       │      GET /api/v1/metrics (Web UI)    │
                       └──────────────────┬───────────────────┘
                                          ▼
                       ┌──────────────────────────────────────┐
                       │   Closed-Loop Webhook Reconciliation │
                       │    (payment_link.paid / captured)    │
                       └──────────────────────────────────────┘
```

### Key Architectural Highlights:
- **FastAPI Core & Async SQLite:** High-concurrency event ingestion pipeline with non-blocking I/O using SQLAlchemy 2.0 and `aiosqlite`.
- **Constant-Time Cryptographic Verification:** Webhook security using `hmac.compare_digest` protecting against timing attacks.
- **Self-Contained Background Tasks:** The decision engine and outreach services execute in isolated sessions via `FastAPI.BackgroundTasks`, acknowledging Razorpay webhooks in `< 15ms`.
- **Append-Only Compliance Ledger:** Every diagnosis, state transition, and customer outreach is recorded in `InterventionAuditLog` with serialized metadata for full auditability.

---

## 🖥️ Live Operations Dashboard & Demo Studio

Revora includes a built-in, Razorpay-caliber dark-mode command center served directly by the FastAPI engine at `http://localhost:8000/`.

- **Real-Time KPI Cards:** Revenue at Risk, Failed Events, In-Recovery Pipeline, and Recovered Revenue.
- **Root-Cause Breakdown:** Live distribution of diagnosed failure reasons.
- **Strategy Routing Analytics:** Real-time breakdown of executed recovery strategies.
- **Track 03 Scenario Studio:** 4 deterministic one-click scenario runners demonstrating adaptive downselling, micro-transaction guardrails, escalation limits, and Gemini outage resilience.

---

## 🚀 Quick Start & Local Setup

### Prerequisites
- Python 3.11+
- Virtualenv

### 1. Clone & Set Up Environment
```bash
git clone https://github.com/sufiyaanmeccai/Revora.git
cd Revora

# Create and activate virtual environment
python -m venv venv

# Windows:
venv\Scripts\activate
# macOS / Linux:
# source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables
```bash
cp .env.example .env

# Optional: Add Razorpay Test Mode credentials to generate live Testnet payment links:
# RAZORPAY_KEY_ID=rzp_test_...
# RAZORPAY_KEY_SECRET=...
# RAZORPAY_WEBHOOK_SECRET=...
# GEMINI_API_KEY=...
# (Default development values work 100% out-of-the-box with offline mocks!)
```

### 3. Run the Test Suite
Revora is backed by a comprehensive suite of **101 automated unit and integration tests**:
```bash
pytest app/tests/ -v
```

### 4. Start the Application Server
```bash
uvicorn app.main:app --reload --port 8000
```

- **Operations Dashboard:** Open [http://localhost:8000/](http://localhost:8000/) in your browser.
- **Interactive API Docs:** Explore the OpenAPI schema at [http://localhost:8000/docs](http://localhost:8000/docs).

---

## 🔍 Technical Honesty: Real vs. Simulated Architecture

To maintain complete transparency for hackathon evaluation:

| Component | Status | Details |
|:---|:---|:---|
| **Razorpay Payment Link API** | **REAL (Test Mode) / MOCK FALLBACK** | Integrates official `razorpay` Python SDK to generate live Test Mode links (`https://rzp.io/i/...`) for `SECURE_PAYMENT_LINK` and `ADAPTIVE_DOWNGRADE_OFFER` when `RAZORPAY_KEY_ID` & `RAZORPAY_KEY_SECRET` exist. Falls back safely to deterministic mock links on missing keys or timeouts. |
| **Webhook Ingestion** | **REAL** | Verifies HMAC-SHA256 signatures with `hmac.compare_digest`, enforces DB-level unique constraint on `x-razorpay-event-id` for deduplication, and processes `payment.failed`, `payment_link.paid`, `payment.captured`, and `subscription.halted`. |
| **AI Recovery Agent (Gemini)** | **REAL** | Official `google-genai` SDK with strict Pydantic structured output (`AgentDecision`), multi-attempt prompt context, and automatic offline `[FALLBACK]` resilience when API key is unset or network fails. |
| **Policy Guardrail Engine** | **REAL** | Strict deterministic Python rule engine acting as final authority: enforces customer consent (Rule 1a), overrides low-ticket downgrades < ₹500 (Rule 1b), caps automated attempts at 2 (Rule 2), and enforces max retries (Rule 3). |
| **Unified Reconciliation** | **REAL** | Matches non-PII identifiers (`reference_id = PaymentEvent.id`), calculates exact 50% downgrade accounting vs 100% full recovery, handles late-arrivals/idempotency, and writes append-only `InterventionAuditLog` entries. |
| **Database & Accounting Layer** | **REAL** | Fully async SQLAlchemy 2.0 ORM with SQLite backend (`PaymentEvent`, `RecoveryWorkflow`, `InterventionAuditLog`). |
| **Operations UI & Scenario Studio** | **REAL** | Interactive dark-mode dashboard and live Scenario Studio served directly by FastAPI. |
| **Demo Studio Scenarios** | **SIMULATED / ISOLATED** | Uses `is_simulated=True` to run completely deterministic offline evaluations during judging without external API dependencies. |
| **Outbound WhatsApp/Voice Gateway** | **MOCKED** | Generates fully formatted WhatsApp payloads and Hinglish voice call scripts without sending live SMS/carrier calls. |
| **Customer Payment Action** | **SIMULATED** | Fast-Forward and Demo Studio simulate customer click-through and payment completion to demonstrate closed-loop reconciliation. |

---

## 🎮 Interactive Demo Studio (Track 03 Scenarios)

The dashboard and API (`/api/v1/demo/scenarios`) include 4 deterministic scenario runners:

1. **Scenario 1: Annual Sub + Insufficient Funds**  
   - *Input:* ₹12,000 failed charge on an annual plan.  
   - *Action:* AI Agent recommends 50% plan downsell (`ADAPTIVE_DOWNGRADE_OFFER`). Guardrail approves (₹12,000 ≥ ₹500). Reconciles customer payment at **₹6,000** (50% captured).
2. **Scenario 2: Micro-Transaction Policy Gate**  
   - *Input:* ₹199 micro-subscription failure.  
   - *Action:* AI suggests downgrade, but **Guardrail Rule 1b** overrides to `SILENT_MANDATE_RETRY` (amount < ₹500).
3. **Scenario 3: Autonomous Escalation Ceiling**  
   - *Input:* Recurring card decline.  
   - *Action:* Attempt 1 times out. On Attempt 2, **Guardrail Rule 2** halts automated recovery (`intervention_count >= 2`) and forces `ESCALATE_TO_HUMAN` to prevent customer harassment.
4. **Scenario 4: Gemini API Outage Resilience**  
   - *Input:* Gateway timeout during an upstream Gemini API 503 fault.  
   - *Action:* Agent catches exception and safely defaults to `SECURE_PAYMENT_LINK` with `[FALLBACK]` audit tag and `confidence_score=0.0`.

---

## 🧪 Running the Test Suite

Revora is validated with **101 automated async pytest tests** covering 100% of the core recovery lifecycle with zero live network dependencies:

```bash
.\venv\Scripts\pytest.exe app/tests/ -v
```

---

## 📜 License
MIT © 2026 Revora Contributors · Built for Razorpay Buildathon 2026

# ⚡ REVORA — Autonomous AI Revenue Recovery Engine

> **Razorpay Buildathon · Track 03: Subscription & Recurring Payment Failures**  
> *An autonomous, agentic revenue recovery system that detects, diagnoses, and recovers involuntary churn through intelligent root-cause routing, silent network backoffs, Hinglish voice outreach, customer value signals, and adaptive downsell mechanics.*

---

## 🎯 The Problem vs. The Revora Solution

### ❌ The Legacy Problem: "Dumb Retries"
Traditional subscription recovery relies on static, periodic dunning schedules (e.g. retry every 24 hours 3 times). This naive approach leads to:
1. **Unnecessary Customer Churn:** Re-attempting an expired card or closed bank account triggers gateway hard-declines and frustrates customers.
2. **Gateway Penalties & Spammed Customers:** Blind WhatsApp and SMS blasts for temporary gateway timeouts ruin customer trust.
3. **Lost Revenue from High-Ticket Failures:** When a user lacks sufficient funds for a ₹10,000 charge, flat retry logic simply fails instead of offering downsell flexibility.
4. **Negative Unit Economics:** Blindly escalating micro-invoices (e.g. ₹100 charge) to manual human operations costs ₹150, destroying profit margins.

### 🚀 The Revora Solution: Semantic Root-Cause Routing & Unit Economics
Revora ingests real-time Razorpay payment lifecycle webhooks with cryptographically verified HMAC-SHA256 signatures, performs semantic error classification, resolves non-PII customer value signals, calculates Net Recovery Value, and executes an economically viable recovery workflow.

---

## 🧩 Core Autonomous Recovery Scenarios

| Failure Pattern | Diagnosed Root Cause | Autonomous Recovery Strategy | Customer Touchpoint |
|:---|:---|:---|:---|
| `GATEWAY_ERROR`, `timeout`, `bank_offline` | **Temporary Network Failure** | `SILENT_MANDATE_RETRY` | **Zero Customer Friction:** Silent exponential retry via Razorpay mandate API (Cost: ₹0.00). |
| `expired_card`, `card_declined`, `invalid_instrument` | **Expired / Invalid Instrument** | `SECURE_PAYMENT_LINK` / `UPI_AUTOPAY_MIGRATION` | **WhatsApp & Hinglish Voice Call:** Generates instant Razorpay Link & guides user to migrate to UPI AutoPay (Cost: ₹2.50). |
| `insufficient_funds`, `low_balance` *(₹ < 500)* | **Low-Ticket Insufficient Funds** | `SILENT_MANDATE_RETRY` | **Scheduled Retry:** Re-attempts charge after a short delay when micro-funds settle (Cost: ₹0.00). |
| `insufficient_funds` *(₹ ≥ 500 High-Ticket)* | **High-Ticket Insufficient Funds** | `ADAPTIVE_DOWNGRADE_OFFER` | **Adaptive Downsell:** Generates instant multi-plan checkout at 50% value to retain customer without churn (Cost: ₹2.50). |
| `insufficient_funds` *(HIGH Tier VIP)* | **VIP Relationship Retention** | `SECURE_PAYMENT_LINK` | **Full Relationship Retention:** Prioritizes full-value checkout over aggressive downselling (Cost: ₹2.50). |

---

## 💡 Customer Value & Unit Economics Intelligence

Revora calculates **Net Recovery Value** across every potential intervention to guarantee economically sound autonomous recovery:

$$\text{Net Recovery Value} = \text{Expected Recoverable Amount} - \text{Simulated Intervention Cost}$$

### Simulated Cost Assumptions (Demo Configuration):
- **`SILENT_MANDATE_RETRY`**: **₹0.00** (Automated backend mandate retry)
- **`SECURE_PAYMENT_LINK`**: **₹2.50** (WhatsApp / SMS outreach + hosted checkout)
- **`UPI_AUTOPAY_MIGRATION`**: **₹2.50** (Interactive WhatsApp flow)
- **`ADAPTIVE_DOWNGRADE_OFFER`**: **₹2.50** (50% downsell checkout link)
- **`ESCALATE_TO_HUMAN`**: **₹150.00** (Human operations support cost)

> [!NOTE]
> All customer value tiers (`HIGH`, `STANDARD`, `LOW`) and channel costs are simulated demo assumptions derived deterministically from non-PII identifiers, not live merchant billing costs or real CRM LTV.

### 🛡️ Strict Guardrail Precedence (Anti-Harassment First):
The `GuardrailEngine` evaluates business, regulatory, and economic rules in strict order:
1. **Priority 1: Safety & Consent (Rule 1a)** — Blocks downgrade/migration without explicit consent → redirects to `SECURE_PAYMENT_LINK`.
2. **Priority 2: Ticket Size Gate (Rule 1b)** — Blocks 50% downsell when amount < ₹500 → overrides to `SILENT_MANDATE_RETRY`.
3. **Priority 3: Economic Viability (Rule 4)** — Blocks interventions with Net Recovery Value $\le 0$ (e.g. ₹150 human escalation on a ₹100 charge) → overrides to `SILENT_MANDATE_RETRY`.
4. **Priority 4: Max Interventions Anti-Harassment (Rule 2/3)** — **RUNS LAST.** If `intervention_count >= 2` or `retry_count >= 3`, strictly forces `ESCALATE_TO_HUMAN` and `ESCALATED_STOPPED`. **Safety and anti-harassment beat economics!**

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
                       └──────┬───────────────────┬───────────┘
                              │                   │
                              ▼                   ▼
           ┌────────────────────────────┐    ┌────────────────────────────┐
           │ Real-Time Ops Dashboard    │    │ Full Audit Trail Export    │
           │ GET /api/v1/metrics (Web UI)│   │ GET /api/v1/audit/export   │
           └────────────────────────────┘    └────────────────────────────┘
                              │
                              ▼
           ┌──────────────────────────────────────┐
           │   Closed-Loop Webhook Reconciliation │
           │    (payment_link.paid / captured)    │
           └──────────────────────────────────────┘
```

---

## 🖥️ Live Operations Dashboard & Demo Studio

Revora includes a built-in dark-mode command center served directly by FastAPI at `http://localhost:8000/`.

- **Real-Time KPI Cards:** Revenue at Risk, Failed Events, In-Recovery Pipeline, and Recovered Revenue.
- **Root-Cause Breakdown:** Live distribution of diagnosed failure reasons.
- **Strategy Routing Analytics:** Real-time breakdown of executed recovery strategies.
- **Customer Value & Economic Drawer:** Live inspection of simulated customer tiers, intervention costs, and net recovery values.
- **Track 03 Scenario Studio:** 7 deterministic one-click scenario runners.
- **Audit Export:** One-click CSV download of the complete append-only audit trail.

---

## 🚀 Quick Start & Local Setup

### Option A: Local Python Setup (Recommended for Dev)

#### 1. Clone & Set Up Virtual Environment
```bash
git clone https://github.com/sufiyaanmeccai/Revora.git
cd Revora

# Create and activate virtual environment
python -m venv venv

# Windows:
venv\Scripts\activate
# macOS / Linux:
# source venv/bin/activate

# Install pinned dependencies
pip install -r requirements.txt
```

#### 2. Configure Environment Variables
```bash
cp .env.example .env

# Optional: Add Razorpay Test Mode credentials to generate live Testnet payment links:
# RAZORPAY_KEY_ID=rzp_test_...
# RAZORPAY_KEY_SECRET=...
# RAZORPAY_WEBHOOK_SECRET=...
# GEMINI_API_KEY=...
# (Default development values work 100% out-of-the-box with offline mocks!)
```

#### 3. Run the Test Suite
```bash
pytest app/tests/ -v
```

#### 4. Start the Application Server
```bash
uvicorn app.main:app --reload --port 8000
```

- **Operations Dashboard:** Open [http://localhost:8000/](http://localhost:8000/) in your browser.
- **Interactive API Docs:** Explore OpenAPI at [http://localhost:8000/docs](http://localhost:8000/docs).
- **Audit CSV Export:** Download directly at [http://localhost:8000/api/v1/audit/export](http://localhost:8000/api/v1/audit/export).

---

### Option B: Docker Container Setup (Reproducible Judge Evaluation)

Revora is fully containerized with zero external service dependencies (pure standalone SQLite + FastAPI):

#### 1. Build and Run with Docker
```bash
# Build the lightweight container
docker build -t revora-engine .

# Run container exposing port 8000
docker run -p 8000:8000 \
  -e RAZORPAY_KEY_ID="rzp_test_..." \
  -e RAZORPAY_KEY_SECRET="..." \
  -e GEMINI_API_KEY="..." \
  revora-engine
```

#### 2. Or Launch with Docker Compose
```bash
docker compose up --build
```
Access the dashboard at `http://localhost:8000/`.

---

## 📊 Auditability & CSV Export

For compliance and evaluation inspection, Revora provides a dedicated endpoint:
- **`GET /api/v1/audit/export`**
  - Streams a clean, deterministic CSV file of the `InterventionAuditLog` table.
  - Columns: `id`, `timestamp`, `workflow_id`, `payment_event_id`, `executed_strategy`, `ai_recommended_strategy`, `ai_confidence`, `guardrail_decision`, `channel`, `intervention_cost`, `net_recovery_value`, `reasoning`.
  - Guaranteed zero-PII and zero-secret leakage.

---

## 🔍 Technical Honesty: Real vs. Simulated Architecture

| Component | Status | Details |
|:---|:---|:---|
| **Razorpay Payment Link API** | **REAL (Test Mode) / MOCK FALLBACK** | Integrates official `razorpay` Python SDK to generate live Test Mode links (`https://rzp.io/i/...`) for `SECURE_PAYMENT_LINK` and `ADAPTIVE_DOWNGRADE_OFFER` when `RAZORPAY_KEY_ID` & `RAZORPAY_KEY_SECRET` exist. Falls back safely to deterministic mock links on missing keys or timeouts. |
| **Webhook Ingestion** | **REAL** | Verifies HMAC-SHA256 signatures with `hmac.compare_digest`, enforces DB-level unique constraint on `x-razorpay-event-id` for deduplication, and processes `payment.failed`, `payment_link.paid`, `payment.captured`, and `subscription.halted`. |
| **AI Recovery Agent (Gemini)** | **REAL** | Official `google-genai` SDK with strict Pydantic structured output (`AgentDecision`), simulated customer context & economic guidance, and automatic offline `[FALLBACK]` resilience when API key is unset or network fails. |
| **Policy Guardrail Engine** | **REAL** | Strict deterministic Python rule engine acting as final authority: enforces customer consent (Priority 1), ₹500 floor (Priority 2), Net Recovery Value viability (Priority 3), and anti-harassment attempt caps (Priority 4). |
| **Unified Reconciliation** | **REAL** | Matches non-PII identifiers (`reference_id = PaymentEvent.id`), calculates exact 50% downgrade accounting vs 100% full recovery, handles late-arrivals/idempotency, and writes append-only `InterventionAuditLog` entries. |
| **Audit Trail CSV Export** | **REAL** | Streams full append-only audit trail via `GET /api/v1/audit/export` without customer PII or credentials. |
| **Database & Accounting Layer** | **REAL** | Fully async SQLAlchemy 2.0 ORM with SQLite backend (`PaymentEvent`, `RecoveryWorkflow`, `InterventionAuditLog`). |
| **Operations UI & Scenario Studio** | **REAL** | Interactive dark-mode dashboard and live Scenario Studio served directly by FastAPI. |
| **Demo Studio Scenarios** | **SIMULATED / ISOLATED** | Uses `is_simulated=True` to run completely deterministic offline evaluations during judging without external API dependencies. |
| **Customer Value & Channel Costs** | **SIMULATED ASSUMPTIONS** | Customer value tiers (`HIGH`, `STANDARD`, `LOW`), account tenure, and channel costs (₹0, ₹2.50, ₹150) are simulated deterministic assumptions for unit-economic intelligence. |
| **Outbound WhatsApp/Voice Gateway** | **MOCKED** | Generates fully formatted WhatsApp payloads and Hinglish voice call scripts without sending live SMS/carrier calls. |
| **Customer Payment Action** | **SIMULATED** | Fast-Forward and Demo Studio simulate customer click-through and payment completion to demonstrate closed-loop reconciliation. |

---

## 🎮 Interactive Demo Studio (Track 03 Scenarios)

The dashboard and API (`/api/v1/demo/scenarios`) include 7 deterministic scenario runners:

1. **Scenario 1: Annual Sub + Insufficient Funds** — ₹12,000 charge → 50% plan downsell → Reconciles ₹6,000.
2. **Scenario 2: Micro-Transaction Policy Gate** — ₹199 charge → Guardrail Rule 1b overrides downgrade to `SILENT_MANDATE_RETRY` (< ₹500).
3. **Scenario 3: Autonomous Escalation Ceiling** — Attempt 2 ceiling hits Guardrail Rule 2 → `ESCALATE_TO_HUMAN`.
4. **Scenario 4: Gemini API Outage Resilience** — Upstream 503 outage → Graceful `[FALLBACK]` payment link.
5. **Scenario 5 (A): VIP Relationship Retention** — ₹15,000 failure on HIGH tier customer → Preserves full value relationship via `SECURE_PAYMENT_LINK` (Net Value: ₹14,997.50).
6. **Scenario 6 (B): Negative Unit Economics Block** — ₹100 failure with proposed human escalation (Cost ₹150) → Priority 3 Guardrail catches negative net value (-₹150 $\le 0$) and overrides to `SILENT_MANDATE_RETRY`.
7. **Scenario 7 (C): Safety Precedence Over Economics** — Customer reached max intervention limit (count $\ge 2$) → Priority 4 Guardrail strictly forces `ESCALATE_TO_HUMAN`. Anti-harassment safety overrides positive unit economics.

---

## 🧪 Running the Test Suite

Revora is validated with **116 automated async pytest tests** covering 100% of the core recovery lifecycle with zero live network dependencies:

```bash
.\venv\Scripts\pytest.exe app/tests/ -v
```

---

## 📜 License
MIT © 2026 Revora Contributors · Built for Razorpay Buildathon 2026

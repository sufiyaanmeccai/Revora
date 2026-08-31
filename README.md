# Revora — Autonomous AI Revenue Recovery Engine

> **Razorpay Buildathon · Track 03 — Subscription & Recurring Payment Failures**

Revora is an agentic AI system that autonomously detects, diagnoses, and recovers
failed subscription payments — eliminating involuntary churn through intelligent
retry orchestration, multi-channel customer communication, and real-time payment
analytics.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| API Framework | FastAPI 0.110+ |
| Runtime | Python 3.11+ / Uvicorn |
| Data Validation | Pydantic v2 |
| Payment Gateway | Razorpay SDK |
| Database | SQLite (dev) → PostgreSQL (prod) |
| AI Orchestration | LangGraph / OpenAI *(Phase 2+)* |

---

## Project Structure

```
Revora/
├── app/
│   ├── main.py              # FastAPI app entry-point
│   ├── core/
│   │   ├── config.py        # Settings (pydantic-settings)
│   │   └── security.py      # Webhook signature verification
│   ├── api/v1/
│   │   ├── api.py           # Router aggregator
│   │   └── endpoints/
│   │       ├── health.py    # GET /api/v1/health
│   │       ├── webhooks.py  # POST /api/v1/webhooks/razorpay
│   │       └── recovery.py  # GET /api/v1/recovery/status
│   ├── models/
│   │   └── schemas.py       # Shared Pydantic schemas
│   ├── services/            # Business logic layer
│   ├── agents/              # AI recovery agents
│   └── tests/
│       └── test_health.py   # Phase 0 health-check tests
├── .env.example
├── requirements.txt
└── README.md
```

---

## Quick Start

```bash
# 1. Clone & enter the project
git clone https://github.com/your-org/revora.git
cd revora

# 2. Create a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your Razorpay credentials

# 5. Run the development server
uvicorn app.main:app --reload

# 6. Run tests
pytest app/tests/
```

API docs are available at **http://localhost:8000/docs**.

---

## Phases

| Phase | Status | Description |
|-------|--------|-------------|
| **0** | ✅ Complete | Scaffolding, config, health check |
| **1** | 🔜 Next | Razorpay webhook ingestion & DB models |
| **2** | 📋 Planned | AI retry-scheduling agent |
| **3** | 📋 Planned | Multi-channel customer communication |
| **4** | 📋 Planned | Analytics dashboard |

---

## License

MIT © Revora Contributors

"""
app/api/v1/api.py
-----------------
Aggregates all v1 endpoint routers into a single APIRouter that is
mounted by the main application.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import demo, health, metrics, recovery, simulation, webhooks

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(webhooks.router)
api_router.include_router(recovery.router)
api_router.include_router(simulation.router)
api_router.include_router(metrics.router)
api_router.include_router(demo.router)


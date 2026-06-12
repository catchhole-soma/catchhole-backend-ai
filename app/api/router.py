from fastapi import APIRouter

from app.api.routes import analysis_jobs, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(analysis_jobs.router, prefix="/analysis-jobs", tags=["analysis-jobs"])

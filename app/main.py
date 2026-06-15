from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.exceptions.handlers import register_exception_handlers


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)
    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()

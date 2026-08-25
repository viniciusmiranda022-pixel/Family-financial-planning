from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.api import router
from app.config import get_settings
from app.db import SessionLocal
from app.models import User

settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    docs_url="/api/docs" if settings.environment != "production" else None,
    redoc_url=None,
)
app.include_router(router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
def health() -> dict:
    return {"status": "healthy", "version": "0.2.0"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    with SessionLocal() as db:
        configured = bool(db.scalar(select(func.count(User.id))))
    return templates.TemplateResponse(request=request, name="index.html", context={"configured": configured})


@app.get("/login")
def login_page() -> RedirectResponse:
    return RedirectResponse(url="/")

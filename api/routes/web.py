from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pathlib import Path

router = APIRouter(tags=["web"])

HTML_DIR = Path(__file__).parent.parent.parent / "web_templates"


@router.get("/ui/", response_class=HTMLResponse)
async def home():
    return (HTML_DIR / "index.html").read_text(encoding="utf-8")


@router.get("/ui/sessions", response_class=HTMLResponse)
async def sessions_page():
    return (HTML_DIR / "sessions.html").read_text(encoding="utf-8")


@router.get("/ui/chat", response_class=HTMLResponse)
async def chat_page():
    return (HTML_DIR / "chat.html").read_text(encoding="utf-8")


@router.get("/ui/quality", response_class=HTMLResponse)
async def quality_page():
    return (HTML_DIR / "quality.html").read_text(encoding="utf-8")
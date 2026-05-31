from fastapi import APIRouter, Depends

from core.analytics import AnalyticsService
from core.security.ratelimit import chat_limiter, rate_limit

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/ask", dependencies=[Depends(rate_limit(chat_limiter))])
async def ask_question(question: dict) -> dict:
    """Ask a question to the AI assistant."""
    svc = AnalyticsService()
    try:
        result = svc.answer_question(question.get("question", ""), last_n=question.get("last_n", 10))
        return {"answer": result.get("direct_answer", ""), "topic": result.get("topic"), "data": result.get("data", {})}
    finally:
        svc.close()
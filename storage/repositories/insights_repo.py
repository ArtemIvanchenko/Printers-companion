"""Insights repository for database operations."""
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.insights import PatternInsight, Hypothesis, LLMRun


class InsightsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def commit(self) -> None:
        self.db.commit()

    def save_insight(self, insight: PatternInsight) -> None:
        existing = self.db.get(PatternInsight, insight.insight_id)
        if existing:
            for key, value in jsonable_encoder(insight).items():
                if key != "insight_id":
                    setattr(existing, key, value)
        else:
            self.db.add(PatternInsight(**jsonable_encoder(insight)))

    def get_insight(self, insight_id: str) -> PatternInsight | None:
        return self.db.get(PatternInsight, insight_id)

    def list_insights(self, session_id: str | None = None) -> list[PatternInsight]:
        stmt = select(PatternInsight)
        if session_id:
            stmt = stmt.where(PatternInsight.session_id == session_id)
        return list(self.db.scalars(stmt.order_by(PatternInsight.created_at.desc())).all())

    def save_hypothesis(self, hypothesis: Hypothesis) -> None:
        existing = self.db.get(Hypothesis, hypothesis.hypothesis_id)
        if existing:
            for key, value in jsonable_encoder(hypothesis).items():
                if key != "hypothesis_id":
                    setattr(existing, key, value)
        else:
            self.db.add(Hypothesis(**jsonable_encoder(hypothesis)))

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        return self.db.get(Hypothesis, hypothesis_id)

    def list_hypotheses(self, session_id: str | None = None) -> list[Hypothesis]:
        stmt = select(Hypothesis)
        if session_id:
            stmt = stmt.where(Hypothesis.session_id == session_id)
        return list(self.db.scalars(stmt.order_by(Hypothesis.created_at.desc())).all())

    def save_llm_run(self, run: LLMRun) -> None:
        existing = self.db.get(LLMRun, run.run_id)
        if existing:
            for key, value in jsonable_encoder(run).items():
                if key != "run_id":
                    setattr(existing, key, value)
        else:
            self.db.add(LLMRun(**jsonable_encoder(run)))

    def get_llm_run(self, run_id: str) -> LLMRun | None:
        return self.db.get(LLMRun, run_id)

    def list_llm_runs(self, session_id: str | None = None) -> list[LLMRun]:
        stmt = select(LLMRun)
        if session_id:
            stmt = stmt.where(LLMRun.session_id == session_id)
        return list(self.db.scalars(stmt.order_by(LLMRun.started_at.desc())).all())
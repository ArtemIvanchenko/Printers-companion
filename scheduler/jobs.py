from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from background_reanalysis.job_planner import plan_historical_reanalysis
from background_reanalysis.scheduler import run_bounded_historical_reanalysis
from core.config.settings import get_settings
from scheduler.schedules import configured_schedules
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


def run_daily_review() -> dict:
    return {
        "processed_sessions": [],
        "new_real_prints": [],
        "service_sessions": [],
        "high_severity_anomalies": [],
        "missing_operator_context": [],
        "new_quality_outcomes": [],
        "reanalyzed_sessions": [],
        "recommended_follow_up_questions": [],
        "failures_or_skipped_items": [],
    }


def run_historical_job() -> dict:
    settings = get_settings()
    plan = plan_historical_reanalysis(
        window_days=settings.historical_reanalysis_window_days,
        max_iterations=settings.historical_reanalysis_max_iterations,
    )
    session_features = _load_session_features()
    return run_bounded_historical_reanalysis(plan, session_features=session_features)


def _load_session_features() -> list[dict[str, object]]:
    try:
        with SessionLocal() as db:
            repo = RuntimeRepository(db)
            return [
                {"session_id": session_id, **(payload.get("group", {}).get("features", {}))}
                for session_id, payload in repo.list_session_payloads()
            ]
    except Exception:
        return []


def main() -> None:
    scheduler = BlockingScheduler()
    settings = get_settings()
    schedules = configured_schedules()
    print(f"Scheduler configured: {schedules}")
    # Honor the configured cron strings instead of hardcoding the hours, so
    # DAILY_REVIEW_CRON / HISTORICAL_REANALYSIS_CRON in .env actually take effect.
    scheduler.add_job(
        run_daily_review,
        CronTrigger.from_crontab(settings.daily_review_cron),
        id="daily_review",
    )
    scheduler.add_job(
        run_historical_job,
        CronTrigger.from_crontab(settings.historical_reanalysis_cron),
        id="historical_reanalysis",
    )
    print(f"Cron schedules: daily_review={settings.daily_review_cron}, historical_reanalysis={settings.historical_reanalysis_cron}")
    scheduler.start()


if __name__ == "__main__":
    main()


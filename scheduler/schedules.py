from core.config.settings import get_settings


def configured_schedules() -> dict[str, str]:
    settings = get_settings()
    return {
        "daily_review": settings.daily_review_cron,
        "historical_reanalysis": settings.historical_reanalysis_cron,
    }


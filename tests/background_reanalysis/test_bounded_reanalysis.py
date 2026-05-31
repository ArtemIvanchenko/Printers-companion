from background_reanalysis.job_planner import plan_historical_reanalysis
from background_reanalysis.scheduler import run_bounded_historical_reanalysis


def test_bounded_reanalysis_caps_iterations_and_handles_insufficient_data() -> None:
    plan = plan_historical_reanalysis(window_days=30, max_iterations=99)
    verdict = run_bounded_historical_reanalysis(plan, [])
    assert plan.max_iterations == 10
    assert verdict["status"] == "insufficient_data"
    assert verdict["completed_iterations"] == 0


def test_reanalysis_stops_when_no_repeated_patterns() -> None:
    plan = plan_historical_reanalysis(window_days=30, max_iterations=10)
    verdict = run_bounded_historical_reanalysis(plan, [{"session_id": "s1", "pause_count": 1}])
    assert verdict["verdict"] == "no_new_pattern"
    assert verdict["completed_iterations"] == 3


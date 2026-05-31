import json
from pathlib import Path

import typer

from background_reanalysis.job_planner import plan_historical_reanalysis
from background_reanalysis.scheduler import run_bounded_historical_reanalysis
from core.config.settings import get_settings
from domain.services.ingestion import IngestionService
from operator_journal.parser import parse_operator_text
from profiles.m350.profile import build_registry, get_profile
from reporting.json_report.generator import generate_session_json_report
from reporting.llm.providers.factory import get_llm_provider
from reporting.markdown_report.generator import generate_markdown_report


app = typer.Typer(help="Industrial printer log analytics CLI")


def _parse_folder(folder: Path):
    return IngestionService(build_registry(), get_profile()).parse(folder)


@app.command("ingest-session")
def ingest_session(folder: Path) -> None:
    result = _parse_folder(folder)
    typer.echo(result.model_dump_json(indent=2))


@app.command("classify-session")
def classify_session(session_id: str) -> None:
    typer.echo(json.dumps({"session_id": session_id, "message": "Use analyze-session with a folder-backed session in this scaffold."}, indent=2))


@app.command("analyze-session")
def analyze_session(session_id: str, folder: Path = typer.Option(..., "--folder")) -> None:
    result = _parse_folder(folder)
    report = generate_session_json_report(session_id, result.files)
    typer.echo(json.dumps(report["session_summary"], indent=2))


@app.command("reanalyze-session")
def reanalyze_session(session_id: str, folder: Path = typer.Option(..., "--folder")) -> None:
    analyze_session(session_id, folder)


@app.command("generate-report")
def generate_report(session_id: str, folder: Path = typer.Option(..., "--folder"), markdown: bool = True) -> None:
    result = _parse_folder(folder)
    report = generate_session_json_report(session_id, result.files)
    typer.echo(generate_markdown_report(report) if markdown else json.dumps(report, indent=2))


@app.command("export-json")
def export_json(session_id: str, folder: Path = typer.Option(..., "--folder")) -> None:
    result = _parse_folder(folder)
    typer.echo(json.dumps(generate_session_json_report(session_id, result.files), indent=2))


@app.command("rebuild-features")
def rebuild_features(session_id: str) -> None:
    typer.echo(json.dumps({"session_id": session_id, "status": "feature_rebuild_requested"}, indent=2))


@app.command("add-operator-event")
def add_operator_event(message: str) -> None:
    typer.echo(parse_operator_text(message).model_dump_json(indent=2))


@app.command("list-operator-events")
def list_operator_events() -> None:
    typer.echo("Operator events are stored in the local database; use API to query them.")


@app.command("add-quality-outcome")
def add_quality_outcome(session_id: str, result: str) -> None:
    typer.echo(json.dumps({"session_id": session_id, "result": result, "status": "draft"}, indent=2))


@app.command("list-quality-outcomes")
def list_quality_outcomes() -> None:
    typer.echo("Quality outcomes are stored in the local database.")


@app.command("import-operator-events")
def import_operator_events(file: Path) -> None:
    count = len(json.loads(file.read_text(encoding="utf-8")))
    typer.echo(json.dumps({"imported_drafts": count}, indent=2))


@app.command("run-daily-review")
def run_daily_review() -> None:
    from scheduler.jobs import run_daily_review as job

    typer.echo(json.dumps(job(), indent=2))


@app.command("run-background-analysis")
def run_background_analysis(window: str = "30d", max_iterations: int = 10) -> None:
    days = int(window.rstrip("d"))
    plan = plan_historical_reanalysis(days, max_iterations)
    typer.echo(json.dumps(run_bounded_historical_reanalysis(plan, []), indent=2))


@app.command("run-pattern-discovery")
def run_pattern_discovery(window: str = "90d") -> None:
    run_background_analysis(window=window, max_iterations=10)


@app.command("list-insights")
def list_insights() -> None:
    typer.echo("Insights are available through /insights after background analysis persists them.")


@app.command("confirm-insight")
def confirm_insight(insight_id: str) -> None:
    typer.echo(json.dumps({"insight_id": insight_id, "status": "confirmation_requires_authorized_API_user"}, indent=2))


@app.command("dismiss-insight")
def dismiss_insight(insight_id: str) -> None:
    typer.echo(json.dumps({"insight_id": insight_id, "status": "dismissal_requires_authorized_API_user"}, indent=2))


@app.command("list-unknown-signals")
def list_unknown_signals() -> None:
    typer.echo("UnknownSignalReport is generated during parsing and historical analysis.")


@app.command("map-signal")
def map_signal(raw_field_name: str, canonical_name: str) -> None:
    profile = get_profile()
    typer.echo(json.dumps({"profile_id": profile.profile_id, "raw_field_name": raw_field_name, "canonical_name": canonical_name, "status": "mapping_draft"}, indent=2))


@app.command("llm-test")
def llm_test() -> None:
    provider = get_llm_provider()
    typer.echo(json.dumps(provider.status(), indent=2))


@app.command("backup-db")
def backup_db() -> None:
    settings = get_settings()
    typer.echo(f"Run: docker compose exec postgres pg_dump -U $POSTGRES_USER {settings.database_url}")


@app.command("backup-minio")
def backup_minio() -> None:
    typer.echo("Run: docker compose exec minio mc mirror /data /backup/minio")


if __name__ == "__main__":
    app()


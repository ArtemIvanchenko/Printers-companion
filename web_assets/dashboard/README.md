# Operator UI modules

`web_templates/dashboard.html` is the screen shell; `dashboard.css` keeps its
existing visual language. The template lists scripts in their dependency order.
They remain ordered classic scripts (`defer`), not ES modules: public functions
are still called by the existing HTML `onclick`/`onchange` contract. Do not add
top-level code that calls a later script before `DOMContentLoaded`.

- `runtime`: common escaping, feedback, workstation headers and optional charts.
- `navigation`: navigation and the seven workflow-screen adapters.
- `catalog`, `archive`: 10-card home pagination and the detailed archive.
- `card-editor`, `card-analysis`, `card-files`: one print's editing, evidence,
  quality, files and log links.
- `history`, `telemetry`: lazy, bounded historical read models and charts.
- `estimate-upload`, `models`: geometry upload, estimates and STL preview.
- `settings`, `operations`, `patterns`, `diagnostics`: the corresponding tools.

The shell performs no SQL reads. Historical panels query `/dashboard/history/*`
only when opened (50 rows per page, hard maximum 100). Inspection pie counts are
SQL aggregates of inspection records, not confirmed ML training outcomes.
Telemetry list/overview uses `/dashboard/telemetry-sessions`: compact saved
statistics, never a fan-out download of all sessions' sensor arrays. The overview
is explicitly limited to the latest 100 sessions; missing values stay unknown.

Vendor resources are versioned under `../vendor/`, including font licenses and
SHA-256 provenance. A missing optional chart/3D library must not prevent catalog,
navigation, editing or upload handlers from initializing.

Regression checks: `tests/test_dashboard_reads.py`, `test_dashboard_template.py`,
`test_dashboard_xss.py` and `test_catalog_frontend.py` (which executes Node tests
under `tests/web/`). Python checks require isolated APP_ENV=test SQLite/outbox.
Shared global state and old inline event attributes are compatibility debt;
this extraction does not claim they have already become isolated components.

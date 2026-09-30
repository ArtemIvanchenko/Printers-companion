"""Inspect the deployed dashboard composition, not a presumed inline script."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def dashboard_source() -> str:
    html = (ROOT / "web_templates/dashboard.html").read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="/assets/(dashboard/[^\"]+)"', html)
    return html + "\n" + "\n".join(
        (ROOT / "web_assets" / name).read_text(encoding="utf-8") for name in assets
    )

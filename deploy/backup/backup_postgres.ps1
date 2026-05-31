param(
  [string]$OutputDir = ".\backups\postgres"
)

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
docker compose exec -T postgres pg_dump -U $env:POSTGRES_USER $env:POSTGRES_DB | Out-File -Encoding utf8 "$OutputDir\printer_logs-$stamp.sql"


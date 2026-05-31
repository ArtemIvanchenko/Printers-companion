param(
  [Parameter(Mandatory=$true)][string]$SqlFile
)

Get-Content -Raw -Path $SqlFile | docker compose exec -T postgres psql -U $env:POSTGRES_USER $env:POSTGRES_DB


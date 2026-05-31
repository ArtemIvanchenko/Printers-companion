param(
  [string]$OutputDir = ".\backups\minio"
)

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
docker compose stop minio
docker run --rm -v printer-log-analytics_minio_data:/data -v ${PWD}\$OutputDir:/backup alpine sh -c "cd /data && tar czf /backup/minio-data.tgz ."
docker compose start minio


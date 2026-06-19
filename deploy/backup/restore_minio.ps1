param(
  [Parameter(Mandatory=$true)][string]$Archive
)

docker compose stop minio
docker run --rm -v printers-companion_minio_data:/data -v ${PWD}:/restore alpine sh -c "rm -rf /data/* && tar xzf /restore/$Archive -C /data"
docker compose start minio


#!/bin/sh
# Paste into DSM Task Scheduler; run manually, not on a schedule.
# Read-only checks. The only write is a NEW report in the project folder.
umask 022
p=${1:-/volume1/docker/printer_companion}
[ "$#" -ne 0 ] || [ -d "$p" ] || p=/volume1/docker/printer-companion
[ -d "$p" ] || { echo 'Project folder not found; no changes made.'; exit 1; }
r="$p/NAS-check-$(date -u +%Y%m%d-%H%M%S)-$$.txt"
(set -C; : > "$r") || exit 1
(
echo 'NAS diagnostics v1 (not installation or repair)'
date -u
uname -m
for f in /proc/sys/kernel/syno_hw_version /etc.defaults/VERSION; do
  [ -r "$f" ] || continue
  case "$f" in
    */VERSION) awk '/^(majorversion|minorversion|productversion|buildnumber)=/' "$f" ;;
    *) head -n 1 "$f" ;;
  esac
done
awk '/model name|Hardware|flags|Features/ {print; if (++n==4) exit}' /proc/cpuinfo 2>/dev/null
awk '/^(MemTotal|MemAvailable|MemFree|SwapFree):/' /proc/meminfo 2>/dev/null
df -h "$p"
echo 'Directories (permissions, NOT a container write test):'
for d in "$p" "$p/postgres" "$p/minio"; do ls -ld "$d"; done
if [ -f "$p/postgres/PG_VERSION" ]; then
  awk '/^[0-9]+([.][0-9]+)?$/ {print "Existing PostgreSQL major:", $0}' "$p/postgres/PG_VERSION"
fi
echo 'Required env keys (values never printed, file never executed):'
if [ -r "$p/.env" ]; then
  awk -F= '
  BEGIN {split("POSTGRES_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD", keys, " ")}
  {k=$1; gsub(/^[ \t]+|[ \t]+$/, "", k)
   for(i=1;i<=3;i++) if(k==keys[i]) {
     n[k]++; v=substr($0,index($0,"=")+1); gsub(/[ \t\r\042\047]/,"",v)
     ok[k]=(length(v)>0 && v !~ /^#/)
   }}
  END {for(i=1;i<=3;i++){k=keys[i]; print k ":", (n[k]>1 ? "DUPLICATE" : (ok[k] ? "PRESENT (not authenticated)" : "MISSING/EMPTY"))}}
  ' "$p/.env"
else echo '.env missing or unreadable'; fi
if grep -Eq 'image:[[:space:]]*minio/minio:' "$p/docker-compose.yml" 2>/dev/null; then
  echo 'WARNING: old MinIO Docker Hub address is still configured'
fi
PATH=$PATH:/usr/local/bin:/var/packages/ContainerManager/target/usr/bin:/var/packages/Docker/target/usr/bin
export PATH
echo 'Docker checks (no pull, start, stop or full inspect):'
if command -v docker >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
  timeout 15 docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'Docker server unavailable'
  timeout 15 docker compose version --short 2>/dev/null || echo 'Compose unavailable'
  timeout 15 docker compose --env-file "$p/.env" -f "$p/docker-compose.yml" config --quiet >/dev/null 2>&1
  echo "Compose validation exit=$? (0=valid; other=error/timeout)"
  for project in printer_companion printer-companion; do
    timeout 15 docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}' 2>/dev/null
    ids=$(timeout 15 docker ps -aq --filter "label=com.docker.compose.project=$project" 2>/dev/null)
    for id in $ids; do
      timeout 10 docker inspect --format '{{.Name}}: status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} {{if .State.Health}}health={{.State.Health.Status}}{{end}}' "$id" 2>/dev/null
    done
  done
else echo 'Docker checks SKIPPED: docker or timeout not available'; fi
echo 'Listening project ports:'
netstat -lnt 2>/dev/null | awk '/:(5433|9000|9001)[[:space:]]/'
echo 'Direct HTTP from NAS, no proxy (registry 401 is normal; NOT an image pull test):'
for url in https://quay.io/v2/ https://registry-1.docker.io/v2/ http://127.0.0.1:9000/minio/health/ready; do
  printf '%s ' "$url"
  curl -q --noproxy '*' --connect-timeout 5 --max-time 10 -s -o /dev/null -w 'HTTP=%{http_code} ' "$url"
  echo "curl_exit=$?"
done
echo 'END. Not tested: client connectivity, passwords, S3 write/read, CPU image execution, firewall rules.'
) > "$r" 2>&1
echo "Report: $r"

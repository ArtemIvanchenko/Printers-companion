#!/bin/bash
# Prepare an INITIAL Synology installation. Never uploads or modifies NAS data.
set -euo pipefail
umask 077

destination=${1:-"$HOME/Desktop/Printer-Companion-NAS"}
revision=e0d59bd13a664383bd18a363df2bf8bab8c3644d
checksum=93b5dad2dde4917f9b6f8b85e2639253bfa841ba4fc87cc065377cb36a38bf73
fail() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }
trap 'printf "\nПодготовка не завершена. Не загружайте неполные файлы на NAS.\n" >&2' ERR
for dependency in curl openssl zip unzip shasum mktemp; do
    command -v "$dependency" >/dev/null || fail "Не найдена программа $dependency."
done
parent=$(dirname "$destination")
[[ -d "$parent" ]] || fail "Нет папки назначения: $parent"
mkdir "$destination.lock" 2>/dev/null || fail "Подготовка уже выполняется или осталась блокировка: $destination.lock"
trap 'rmdir "$destination.lock" 2>/dev/null || true' EXIT

if [[ -e "$destination" ]]; then
    [[ -f "$destination/.prepared-v1" && -f "$destination/NAS-files.zip" ]] ||
        fail "Папка уже существует, но это не готовый комплект. Она сохранена: $destination"
    (cd "$destination" && shasum -a 256 -c .bundle.sha256 >/dev/null &&
        unzip -tq NAS-files.zip >/dev/null) || fail "Комплект изменён или повреждён. Пароли не заменены."
    printf 'Используется готовый комплект. Пароли сохранены.\n'
else
    staging=$(mktemp -d "$parent/.nas-preparation.XXXXXX")
    curl --fail --location --silent --show-error --retry 3 \
        --connect-timeout 15 --max-time 90 \
        "https://raw.githubusercontent.com/ArtemIvanchenko/Printers-companion/$revision/deploy/nas/docker-compose.yml" \
        --output "$staging/docker-compose.yml"
    actual=$(shasum -a 256 "$staging/docker-compose.yml")
    [[ "${actual%% *}" == "$checksum" ]] || fail "Контрольная сумма конфигурации не совпала."
    postgres_password=$(openssl rand -hex 20)
    minio_password=$(openssl rand -hex 20)
    [[ ${#postgres_password} -eq 40 && ${#minio_password} -eq 40 ]] || fail "Не удалось создать пароли."
    printf '%s\n' 'POSTGRES_DB=printer_logs' 'POSTGRES_USER=printer_logs' \
        "POSTGRES_PASSWORD=$postgres_password" 'MINIO_ROOT_USER=printer-companion-admin' \
        "MINIO_ROOT_PASSWORD=$minio_password" > "$staging/.env"
    unset postgres_password minio_password
    (
        cd "$staging"
        zip -q NAS-files.zip docker-compose.yml .env
        unzip -tq NAS-files.zip >/dev/null
        shasum -a 256 docker-compose.yml .env NAS-files.zip > .bundle.sha256
        printf '%s\n' "$revision" > .prepared-v1
    )
    mv "$staging" "$destination"
    printf 'Комплект создан и проверен.\n'
fi
printf '\nАрхив: %s/NAS-files.zip\n' "$destination"
printf '%s\n' \
    'Только для первоначальной установки, пока хранилища ещё не запускались.' \
    'Загрузите ZIP в File Station: docker/printer_companion → Извлечь здесь.' \
    'Подтвердите замену только docker-compose.yml и .env.' \
    'Затем пересоберите проект printer_companion в Container Manager.' \
    'Ожидаются два контейнера: postgres и minio.' \
    'Архив содержит пароли: сохраните его, не отправляйте в общий чат.' \
    'Для уже работающей базы не заменяйте .env новым комплектом.'
if [[ $(uname -s) == Darwin ]]; then
    open -R "$destination/NAS-files.zip" || true
fi

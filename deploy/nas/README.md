# Перенос Postgres и MinIO на общий NAS (Synology DS224+)

Статус: подготовлено заранее, доступа к NAS пока нет. Ничего из этого файла
не влияет на текущую работу приложения — все команды выполняются только
на момент реального переключения.

## 0. Что уже готово в репозитории

- `deploy/nas/docker-compose.yml` — Postgres + MinIO для запуска на самом NAS.
- `deploy/nas/.env.nas.example` — шаблон переменных окружения для NAS-стороны.
- `deploy/nas/docker-compose.client-remote-db.yml` — оверрайд для клиентских ПК,
  отключает локальные postgres/minio и переводит приложение на NAS.
- `deploy/backup/backup_postgres.ps1`, `deploy/backup/backup_minio.ps1` — уже
  существующие скрипты для снятия дампа/слепка с текущего ПК.

## 1. Когда появится доступ к NAS

1. Обновить DSM до актуальной версии.
2. Установить пакет **Container Manager** (Package Center).
3. При необходимости — апгрейд RAM DS224+ до 6GB (иначе Postgres+MinIO
   вместе будут упираться в память при 2GB).
4. Создать том (желательно RAID1 из двух дисков) и папки:
   ```
   /volume1/docker/printer-companion/postgres
   /volume1/docker/printer-companion/minio
   ```
5. NAS стоит дома у начальника, не в одной сети с рабочими ПК — обычный
   локальный IP не поможет. Установить пакет **Tailscale** (Package Center),
   авторизовать NAS в общей tailnet. Получить постоянный Tailscale-адрес
   вида `100.x.x.x` — далее обозначается как `<NAS_IP>`.
6. Порты наружу в интернет не пробрасывать — весь трафик идёт через
   Tailscale-туннель. В брандмауэре DSM дополнительно ограничить доступ к
   `5433` и `9000/9001` диапазоном Tailscale-сети `100.64.0.0/10`.
7. На каждом рабочем ПК тоже установить Tailscale (tailscale.com/download)
   и авторизовать в той же tailnet — иначе ПК не увидит NAS.

## 2. Запуск Postgres и MinIO на NAS

По SSH на NAS (или через Container Manager → Project):

```bash
cd /volume1/docker/printer-companion
cp deploy/nas/.env.nas.example .env
# отредактировать .env — задать реальные пароли
docker compose -f deploy/nas/docker-compose.yml up -d
```

Проверить:
```bash
docker compose -f deploy/nas/docker-compose.yml ps
```

## 3. Перенос данных

Решено начать с чистого листа — старые локальные данные на ПК никуда не
переносятся. Новый Postgres/MinIO на NAS стартует пустым, вся новая работа
(карточки печати, файлы) с этого момента копится сразу централизованно.
Старые локальные volume на ПК можно оставить нетронутыми на всякий случай
(ничего с ними делать не нужно, они просто перестанут использоваться после
шага 4).

## 4. Переключение клиентских ПК

На каждом ПК:

1. В `.env` заменить:
   ```
   DATABASE_URL=postgresql+psycopg://printer_logs:<пароль>@<NAS_IP>:5433/printer_logs
   MINIO_ENDPOINT=<NAS_IP>:9000
   ```
   (пароли — те же, что заданы в `.env` на NAS на шаге 2).

2. Запустить приложение с оверрайдом:
   ```powershell
   docker compose -f docker-compose.yml -f deploy\nas\docker-compose.client-remote-db.yml up -d
   ```

3. Проверить логи `api`/`worker` на успешное подключение к Postgres/MinIO.
4. Убедиться, что карточка печати, добавленная на одном ПК, видна на
   остальных.

Локальный `postgres_data`/`minio_data` volume на ПК **не удалять** минимум
неделю — план отката: убрать оверрайд-файл из команды запуска, вернуться
к локальным контейнерам.

## 5. После переключения

- Настроить Hyper Backup DSM — регулярный бэкап
  `/volume1/docker/printer-companion/postgres` и `.../minio`.
- Настроить Resource Monitor DSM — алерты по RAM/CPU/диску, чтобы вовремя
  заметить нехватку ресурсов на DS224+.

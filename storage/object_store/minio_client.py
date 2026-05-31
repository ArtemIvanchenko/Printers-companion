from pathlib import Path

from minio import Minio
from minio.error import S3Error

from core.config.settings import Settings, get_settings


class ObjectStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = Minio(
            self.settings.minio_endpoint,
            access_key=self.settings.minio_root_user,
            secret_key=self.settings.minio_root_password,
            secure=self.settings.minio_secure,
        )

    def ensure_bucket(self, bucket: str) -> None:
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def put_file(self, bucket: str, object_name: str, path: Path) -> str:
        self.ensure_bucket(bucket)
        self.client.fput_object(bucket, object_name, str(path))
        return f"s3://{bucket}/{object_name}"

    def is_available(self) -> bool:
        try:
            self.client.list_buckets()
            return True
        except S3Error:
            return False
        except Exception:
            return False


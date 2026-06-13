import io
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

    def ensure_all_buckets(self) -> None:
        """Create every bucket the application uses (idempotent, best-effort)."""
        for bucket in (
            self.settings.minio_bucket_raw,
            self.settings.minio_bucket_reports,
            self.settings.minio_bucket_stls,
            self.settings.minio_bucket_magics,
            self.settings.minio_bucket_photos,
            self.settings.minio_bucket_docs,
        ):
            self.ensure_bucket(bucket)

    def put_file(self, bucket: str, object_name: str, path: Path) -> str:
        self.ensure_bucket(bucket)
        self.client.fput_object(bucket, object_name, str(path))
        return f"s3://{bucket}/{object_name}"

    def put_bytes(
        self, bucket: str, object_name: str, data: bytes,
        content_type: str = "application/json",
    ) -> str:
        """Upload an in-memory blob; returns its s3://bucket/object URI."""
        self.ensure_bucket(bucket)
        self.client.put_object(
            bucket, object_name, io.BytesIO(data), length=len(data), content_type=content_type,
        )
        return f"s3://{bucket}/{object_name}"

    def get_bytes(self, bucket: str, object_name: str) -> bytes | None:
        """Download an object's bytes, or None if missing/unavailable."""
        try:
            response = self.client.get_object(bucket, object_name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except Exception:
            return None

    def remove_object(self, bucket: str, object_name: str) -> bool:
        """Delete an object; True on success, False if missing/unavailable."""
        try:
            self.client.remove_object(bucket, object_name)
            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        try:
            self.client.list_buckets()
            return True
        except S3Error:
            return False
        except Exception:
            return False


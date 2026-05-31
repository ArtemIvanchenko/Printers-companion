from collections.abc import Generator
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from core.config.settings import get_settings
from storage.db.init_db import create_all
from storage.db.session import get_db
from storage.repositories.runtime import RuntimeRepository


@lru_cache(maxsize=1)
def ensure_local_schema() -> None:
    if get_settings().app_env == "local":
        create_all()


def get_runtime_repository(db: Session = Depends(get_db)) -> Generator[RuntimeRepository, None, None]:
    ensure_local_schema()
    yield RuntimeRepository(db)

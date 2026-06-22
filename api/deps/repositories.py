from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from storage.db.session import get_db
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.runtime import RuntimeRepository


def get_runtime_repository(db: Session = Depends(get_db)) -> Generator[RuntimeRepository, None, None]:
    yield RuntimeRepository(db)


def get_prints_repository(db: Session = Depends(get_db)) -> Generator[PrintsRepository, None, None]:
    yield PrintsRepository(db)

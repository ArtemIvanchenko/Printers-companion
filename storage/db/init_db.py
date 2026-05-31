from storage.db.base import Base
from storage.db.session import engine

# Import model modules so SQLAlchemy metadata is populated.
import domain.models.entities  # noqa: F401


def create_all() -> None:
    Base.metadata.create_all(bind=engine)


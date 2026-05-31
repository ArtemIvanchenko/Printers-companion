from dataclasses import dataclass
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Query

T = TypeVar("T")

# Reusable query-parameter annotations. Using Annotated (instead of `= Query(...)`)
# keeps the real int default in the signature, so handlers stay directly callable
# in unit tests while FastAPI still applies the validation constraints.
SkipParam = Annotated[int, Query(ge=0, description="Number of records to skip")]
LimitParam = Annotated[int, Query(ge=1, le=1000, description="Max records to return")]


@dataclass
class PaginationParams:
    skip: int = 0
    limit: int = 100

    @classmethod
    def from_query(cls, skip: SkipParam = 0, limit: LimitParam = 100) -> "PaginationParams":
        return cls(skip=skip, limit=limit)


@dataclass
class PaginatedResponse(Generic[T]):
    items: list[T]
    total: int
    skip: int
    limit: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "skip": self.skip,
            "limit": self.limit,
            "returned": len(self.items),
        }

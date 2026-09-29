"""ORM models. Import every model here so Alembic autogenerate sees it (tables arrive in P1)."""

from app.db.base import Base

__all__ = ["Base"]

# Import every model here so they all register on Base.metadata.
# Alembic imports this package to see the full set of tables.
from app.models.user import User

__all__ = ["User"]

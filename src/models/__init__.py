"""
models package — SQLAlchemy ORM models (the database tables as Python classes).

WHY THIS __init__ IMPORTS EVERYTHING:
  Alembic's autogenerate and SQLAlchemy's Base.metadata.create_all() can only
  "see" a table if the class that defines it has been IMPORTED (importing the
  class is what registers it on Base.metadata). By re-exporting every model
  here, any code that does `from src.models import Base, User, Task, ...` (or
  just imports this package) guarantees ALL tables are registered. This avoids
  the classic "Alembic didn't detect my new table" bug.

DON'T CONFUSE THESE WITH PYDANTIC SCHEMAS:
  - ORM models (here)   = how data is stored in the DATABASE (tables, columns,
                          foreign keys, relationships).
  - Pydantic schemas (src/schemas) = how data enters/leaves the API over HTTP
                          (request/response shapes, validation).
  Keeping them separate is deliberate: the DB shape and the API shape evolve
  independently (e.g. you never want to send password_hash over the wire).
"""

from src.models.base import Base
from src.models.comment import Comment
from src.models.project import Project
from src.models.task import Task, TaskStatus
from src.models.user import User

# __all__ documents the public surface of this package (what `from ... import *`
# would pull, and a hint to readers/tools about what's intended for external use).
__all__ = ["Base", "User", "Project", "Task", "TaskStatus", "Comment"]

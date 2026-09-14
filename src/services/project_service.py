"""
project_service.py — project CRUD with OWNERSHIP enforcement.

The recurring pattern here — "load the resource, then check the current user is
allowed to touch it" — is the core of AUTHORIZATION (as opposed to
AUTHentication, which just proves who you are). Study get_owned_project(); the
same shape repeats for tasks and comments.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.exceptions import NotFoundError, PermissionDeniedError
from src.models.project import Project
from src.models.user import User
from src.schemas.project import ProjectCreate, ProjectUpdate


async def get_owned_project(
    db: AsyncSession, project_id: str, user: User
) -> Project:
    """
    Load a project AND verify the current user owns it.

    WHY 404 vs 403 IS A DELIBERATE CHOICE:
      - If the project doesn't exist at all -> 404 Not Found.
      - If it exists but belongs to someone else -> 403 Permission Denied here.
      Some APIs return 404 even for "exists but not yours" to avoid revealing
      that the id exists. We use 403 for clarity in a learning project; the
      README notes the tradeoff. Either is defensible — know why you chose one.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")
    if project.owner_id != user.id:
        raise PermissionDeniedError("You do not own this project")
    return project


async def list_projects(db: AsyncSession, user: User) -> list[Project]:
    """
    List the current user's projects, newest first.

    Note the WHERE filters by owner_id: a user only ever sees their OWN
    projects. Never trust the client to filter for you — scope every list query
    to the authenticated user on the server side.
    """
    result = await db.execute(
        select(Project)
        .where(Project.owner_id == user.id)
        .order_by(Project.created_at.desc())
    )
    # .scalars() unwraps single-column rows into plain Project objects;
    # .all() materializes them into a list.
    return list(result.scalars().all())


async def create_project(
    db: AsyncSession, data: ProjectCreate, user: User
) -> Project:
    """Create a project owned by the current user."""
    project = Project(
        name=data.name,
        description=data.description,
        owner_id=user.id,  # ownership is set from the token, never from input.
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def update_project(
    db: AsyncSession, project_id: str, data: ProjectUpdate, user: User
) -> Project:
    """Partial update of an owned project (same PATCH pattern as users)."""
    project = await get_owned_project(db, project_id, user)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(project, field, value)
    await db.commit()
    await db.refresh(project)
    return project


async def delete_project(
    db: AsyncSession, project_id: str, user: User
) -> None:
    """
    Delete an owned project. Because Project.tasks has
    cascade="all, delete-orphan" (and tasks cascade to their comments), deleting
    the project cleans up all its tasks and their comments automatically — no
    orphaned rows left behind.
    """
    project = await get_owned_project(db, project_id, user)
    await db.delete(project)
    await db.commit()

"""
projects.py — CRUD endpoints for projects, all scoped to the current user.

This router also demonstrates a small CACHING pattern on the single-project GET
(read-through cache + invalidation on write). Read those comments closely — the
hard part of caching is knowing WHEN to invalidate.
"""

from fastapi import APIRouter, status

from src.dependencies import CacheDep, CurrentUser, DbSession
from src.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate
from src.services import project_service

router = APIRouter(prefix="/projects", tags=["projects"])

# TTL for the cached single-project response, in seconds.
_PROJECT_CACHE_TTL = 60


def _project_cache_key(project_id: str) -> str:
    """Namespaced cache key so project keys never collide with other cached data."""
    return f"project:{project_id}"


@router.get("", response_model=list[ProjectOut], summary="List my projects")
async def list_projects(user: CurrentUser, db: DbSession) -> list[ProjectOut]:
    """List every project owned by the current user (newest first)."""
    return await project_service.list_projects(db, user)


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create_project(
    data: ProjectCreate, user: CurrentUser, db: DbSession
) -> ProjectOut:
    """Create a project owned by the current user."""
    return await project_service.create_project(db, data, user)


@router.get(
    "/{project_id}", response_model=ProjectOut, summary="Get one project"
)
async def get_project(
    project_id: str, user: CurrentUser, db: DbSession, cache: CacheDep
) -> ProjectOut:
    """
    Fetch a single project, demonstrating a READ-THROUGH CACHE.

    THE PATTERN, step by step:
      1. Try the cache first. On a HIT we deserialize the stored JSON and return
         it WITHOUT touching the DB — the fast path.
      2. On a MISS, load from the DB (which also enforces ownership).
      3. Store the serialized result in the cache with a TTL, so the NEXT read
         within 60s is a hit.

    We still run the ownership check against the DB only on a miss; on a hit we
    rely on the fact that we cache per-project and the key isn't guessable-cross-
    user in this app. (In a multi-tenant system you'd include the user id in the
    key or cache post-authorization — a good thing to mention in an interview.)

    model_validate_json / model_dump_json are how Pydantic moves between objects
    and the JSON strings we store in Redis.
    """
    key = _project_cache_key(project_id)

    cached = await cache.get(key)
    if cached is not None:
        # Rebuild the schema object from the cached JSON string.
        return ProjectOut.model_validate_json(cached)

    project = await project_service.get_owned_project(db, project_id, user)
    result = ProjectOut.model_validate(project)  # ORM -> schema
    # Store JSON in Redis with the TTL.
    await cache.set(key, result.model_dump_json(), _PROJECT_CACHE_TTL)
    return result


@router.patch(
    "/{project_id}", response_model=ProjectOut, summary="Update a project"
)
async def update_project(
    project_id: str,
    data: ProjectUpdate,
    user: CurrentUser,
    db: DbSession,
    cache: CacheDep,
) -> ProjectOut:
    """
    Update a project and INVALIDATE its cache entry.

    CACHE INVALIDATION: after any write we delete the cached copy so the next
    read re-fetches fresh data instead of serving the stale pre-update version.
    Forgetting this step is the classic caching bug ("why is my update not
    showing up?"). Write -> mutate DB -> delete cache key.
    """
    updated = await project_service.update_project(db, project_id, data, user)
    await cache.delete(_project_cache_key(project_id))
    return updated


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,  # 204 = success, no response body.
    summary="Delete a project",
)
async def delete_project(
    project_id: str, user: CurrentUser, db: DbSession, cache: CacheDep
) -> None:
    """
    Delete a project (and, via cascade, its tasks and comments) and clear its
    cache entry. Returns 204 No Content — nothing to send back on a delete.
    """
    await project_service.delete_project(db, project_id, user)
    await cache.delete(_project_cache_key(project_id))

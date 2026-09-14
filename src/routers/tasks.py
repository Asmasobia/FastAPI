"""
tasks.py — task endpoints. The busiest router: it shows QUERY-PARAM FILTERING,
CURSOR PAGINATION, path-nested creation, and BACKGROUND TASKS.

Endpoint map:
  POST   /projects/{project_id}/tasks   create a task in a project
  GET    /tasks                         list/filter/paginate my tasks
  GET    /tasks/{task_id}               one task WITH comments (eager-loaded)
  PATCH  /tasks/{task_id}               partial update
  DELETE /tasks/{task_id}               delete
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, status

from src.dependencies import CurrentUser, DbSession
from src.models.task import TaskStatus
from src.notifications import send_task_assigned_email
from src.schemas.common import Page
from src.schemas.task import TaskCreate, TaskDetailOut, TaskOut, TaskUpdate
from src.services import task_service

router = APIRouter(tags=["tasks"])


@router.post(
    "/projects/{project_id}/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task in a project",
)
async def create_task(
    project_id: str,
    data: TaskCreate,
    user: CurrentUser,
    db: DbSession,
    # BackgroundTasks is injected by FastAPI when you declare a param of this
    # type. You register callables on it; FastAPI runs them AFTER the response
    # is sent. No Depends() needed — FastAPI recognizes the type specially.
    background: BackgroundTasks,
) -> TaskOut:
    """
    Create a task. If it's assigned to someone, fire a background "email".

    WHY BACKGROUND:
      The client shouldn't wait for a (simulated) email round-trip to get its
      201. We register the notification to run after the response is flushed.
      background.add_task(fn, **kwargs) schedules fn(**kwargs) to run later, in
      this same process. Fire-and-forget; see notifications.py for the caveats
      (no retries/durability — use Celery for must-happen work).
    """
    task = await task_service.create_task(db, project_id, data, user)

    if task.assignee_id is not None:
        # Schedule, don't await. Runs after the response is returned.
        background.add_task(
            send_task_assigned_email,
            to_user_id=task.assignee_id,
            task_id=task.id,
            task_title=task.title,
        )

    return task


@router.get("/tasks", response_model=Page[TaskOut], summary="List/filter tasks")
async def list_tasks(
    user: CurrentUser,
    db: DbSession,
    # ---- QUERY PARAMETERS ----
    # A parameter that isn't part of the path and has a scalar type becomes a
    # QUERY parameter (?status=todo&limit=20). Query(...) lets us add validation
    # + docs. All are optional here (default None / a default value).
    #
    # `status: TaskStatus | None` — because the type is the enum, FastAPI both
    # validates the value is a real status AND renders a dropdown in /docs.
    status: Annotated[
        TaskStatus | None,
        Query(description="Filter by task status (todo/in_progress/done)."),
    ] = None,
    project_id: Annotated[
        str | None, Query(description="Filter to a single project.")
    ] = None,
    assignee_id: Annotated[
        str | None, Query(description="Filter by assignee user id.")
    ] = None,
    cursor: Annotated[
        str | None,
        Query(description="Opaque pagination cursor from a previous response."),
    ] = None,
    # ge/le bound the page size so a client can't request a million rows.
    limit: Annotated[
        int, Query(ge=1, le=100, description="Max items per page.")
    ] = 20,
) -> Page[TaskOut]:
    """
    List the current user's tasks with optional filters and cursor pagination.

    HOW A CLIENT PAGES:
      1. GET /tasks?limit=20  -> returns 20 items + a next_cursor.
      2. GET /tasks?limit=20&cursor=<that cursor>  -> the next 20.
      3. Repeat until next_cursor is null (no more pages).
    Filters (status/project_id/assignee_id) can be combined freely; they're all
    ANDed together in the service.

    We wrap the service's (items, next_cursor) tuple in the Page envelope so the
    response is self-describing (items + next_cursor + count).
    """
    items, next_cursor = await task_service.list_tasks(
        db,
        user,
        project_id=project_id,
        status=status,
        assignee_id=assignee_id,
        cursor=cursor,
        limit=limit,
    )
    return Page[TaskOut](items=items, next_cursor=next_cursor, count=len(items))


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailOut,
    summary="Get one task with its comments",
)
async def get_task(
    task_id: str, user: CurrentUser, db: DbSession
) -> TaskDetailOut:
    """
    Fetch a single task WITH its comments. The service eager-loads comments via
    selectinload (no N+1), and TaskDetailOut serializes the nested comments list.
    """
    return await task_service.get_task_detail(db, task_id, user)


@router.patch(
    "/tasks/{task_id}", response_model=TaskOut, summary="Update a task"
)
async def update_task(
    task_id: str, data: TaskUpdate, user: CurrentUser, db: DbSession
) -> TaskOut:
    """Partial update of a task the user owns (via its project)."""
    return await task_service.update_task(db, task_id, data, user)


@router.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task",
)
async def delete_task(
    task_id: str, user: CurrentUser, db: DbSession
) -> None:
    """Delete a task (cascade removes its comments). Returns 204."""
    await task_service.delete_task(db, task_id, user)

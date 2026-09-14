"""
test_tasks.py — task CRUD, plus the two showcase features: FILTERING and
CURSOR PAGINATION. The pagination test walks the cursor to the end and asserts
we get every item exactly once — the real proof that keyset pagination works.
"""

import pytest_asyncio


@pytest_asyncio.fixture
async def project_id(auth_client):
    """Convenience fixture: create a project and return its id for task tests."""
    resp = await auth_client.post("/projects", json={"name": "Task Project"})
    return resp.json()["id"]


async def test_create_task(auth_client, project_id):
    resp = await auth_client.post(
        f"/projects/{project_id}/tasks",
        json={"title": "Do the thing", "priority": 2},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Do the thing"
    assert body["status"] == "todo"  # default applied
    assert body["priority"] == 2


async def test_create_task_invalid_priority_422(auth_client, project_id):
    # priority must be 1..5; 9 is out of range -> 422 (Field(ge=1, le=5)).
    resp = await auth_client.post(
        f"/projects/{project_id}/tasks",
        json={"title": "Bad", "priority": 9},
    )
    assert resp.status_code == 422


async def test_create_task_blank_title_422(auth_client, project_id):
    # Whitespace-only title is rejected by our field_validator.
    resp = await auth_client.post(
        f"/projects/{project_id}/tasks",
        json={"title": "   "},
    )
    assert resp.status_code == 422


async def test_get_task_detail_includes_comments(auth_client, project_id):
    tid = (
        await auth_client.post(
            f"/projects/{project_id}/tasks", json={"title": "With comments"}
        )
    ).json()["id"]
    # Add two comments.
    await auth_client.post(f"/tasks/{tid}/comments", json={"body": "one"})
    await auth_client.post(f"/tasks/{tid}/comments", json={"body": "two"})

    resp = await auth_client.get(f"/tasks/{tid}")
    assert resp.status_code == 200
    body = resp.json()
    # The detail schema embeds comments (eager-loaded, no N+1).
    assert len(body["comments"]) == 2
    assert {c["body"] for c in body["comments"]} == {"one", "two"}


async def test_update_task_status(auth_client, project_id):
    tid = (
        await auth_client.post(
            f"/projects/{project_id}/tasks", json={"title": "T"}
        )
    ).json()["id"]
    resp = await auth_client.patch(
        f"/tasks/{tid}", json={"status": "in_progress"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


async def test_empty_patch_rejected_422(auth_client, project_id):
    # Our model_validator forbids an empty PATCH body.
    tid = (
        await auth_client.post(
            f"/projects/{project_id}/tasks", json={"title": "T"}
        )
    ).json()["id"]
    resp = await auth_client.patch(f"/tasks/{tid}", json={})
    assert resp.status_code == 422


async def test_delete_task(auth_client, project_id):
    tid = (
        await auth_client.post(
            f"/projects/{project_id}/tasks", json={"title": "Del"}
        )
    ).json()["id"]
    resp = await auth_client.delete(f"/tasks/{tid}")
    assert resp.status_code == 204
    resp = await auth_client.get(f"/tasks/{tid}")
    assert resp.status_code == 404


async def test_filter_by_status(auth_client, project_id):
    # Create tasks in different statuses.
    await auth_client.post(
        f"/projects/{project_id}/tasks",
        json={"title": "todo one", "status": "todo"},
    )
    await auth_client.post(
        f"/projects/{project_id}/tasks",
        json={"title": "done one", "status": "done"},
    )
    # Filter to only "done".
    resp = await auth_client.get("/tasks?status=done")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "done"


async def test_cursor_pagination_walks_all_items(auth_client, project_id):
    """
    Create 5 tasks, page through with limit=2, and assert we see all 5 exactly
    once across pages. This is the definitive pagination correctness test.
    """
    created_titles = set()
    for i in range(5):
        title = f"task-{i}"
        created_titles.add(title)
        await auth_client.post(
            f"/projects/{project_id}/tasks", json={"title": title}
        )

    seen_titles = set()
    cursor = None
    pages = 0
    while True:
        url = "/tasks?limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        resp = await auth_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            # Each item must appear exactly once — no duplicates across pages.
            assert item["title"] not in seen_titles
            seen_titles.add(item["title"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:  # no more pages
            break
        assert pages < 10  # safety: never loop forever

    # We paged through every created task, once each.
    assert seen_titles == created_titles
    # 5 items at 2 per page => 3 pages (2 + 2 + 1).
    assert pages == 3


async def test_limit_validation_422(auth_client):
    # limit is bounded 1..100; 0 and 999 are invalid.
    assert (await auth_client.get("/tasks?limit=0")).status_code == 422
    assert (await auth_client.get("/tasks?limit=999")).status_code == 422

"""
test_comments.py — comment creation/listing and author-is-current-user behavior.
"""

import pytest_asyncio


@pytest_asyncio.fixture
async def task_id(auth_client):
    """Create a project + task and return the task id."""
    pid = (
        await auth_client.post("/projects", json={"name": "Comment Project"})
    ).json()["id"]
    tid = (
        await auth_client.post(
            f"/projects/{pid}/tasks", json={"title": "Commentable"}
        )
    ).json()["id"]
    return tid


async def test_add_and_list_comments(auth_client, task_id):
    r1 = await auth_client.post(
        f"/tasks/{task_id}/comments", json={"body": "first"}
    )
    assert r1.status_code == 201
    # The author must be set to the current user, NOT anything from the body.
    me = (await auth_client.get("/users/me")).json()
    assert r1.json()["author_id"] == me["id"]

    await auth_client.post(f"/tasks/{task_id}/comments", json={"body": "second"})

    resp = await auth_client.get(f"/tasks/{task_id}/comments")
    assert resp.status_code == 200
    bodies = [c["body"] for c in resp.json()]
    # Ordered oldest-first (as the service orders them).
    assert bodies == ["first", "second"]


async def test_empty_comment_body_422(auth_client, task_id):
    resp = await auth_client.post(f"/tasks/{task_id}/comments", json={"body": ""})
    assert resp.status_code == 422


async def test_comment_on_missing_task_404(auth_client):
    resp = await auth_client.post(
        "/tasks/nope/comments", json={"body": "hi"}
    )
    assert resp.status_code == 404

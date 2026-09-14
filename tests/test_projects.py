"""
test_projects.py — project CRUD and, importantly, AUTHORIZATION (ownership).

The ownership tests are the most valuable here: they prove one user can't read
or modify another user's projects. That's the kind of access-control test
interviewers love to see, because it's a common real-world security bug.
"""

import pytest_asyncio


async def test_create_and_get_project(auth_client):
    # Create.
    resp = await auth_client.post("/projects", json={"name": "My Project"})
    assert resp.status_code == 201
    project = resp.json()
    assert project["name"] == "My Project"
    pid = project["id"]

    # Fetch it back.
    resp = await auth_client.get(f"/projects/{pid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == pid


async def test_list_projects_only_shows_mine(auth_client):
    await auth_client.post("/projects", json={"name": "P1"})
    await auth_client.post("/projects", json={"name": "P2"})
    resp = await auth_client.get("/projects")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert names == {"P1", "P2"}


async def test_update_project(auth_client):
    pid = (await auth_client.post("/projects", json={"name": "Old"})).json()["id"]
    resp = await auth_client.patch(
        f"/projects/{pid}", json={"name": "New Name"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"


async def test_delete_project(auth_client):
    pid = (await auth_client.post("/projects", json={"name": "Doomed"})).json()["id"]
    resp = await auth_client.delete(f"/projects/{pid}")
    assert resp.status_code == 204  # No Content.
    # Now it should be gone.
    resp = await auth_client.get(f"/projects/{pid}")
    assert resp.status_code == 404


async def test_get_missing_project_404(auth_client):
    resp = await auth_client.get("/projects/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# The ownership/authorization test: a SECOND user must not touch the first
# user's project. We need a second authenticated client for this, so we build
# one inline from the base `client` fixture.
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def second_auth_client(client):
    """A second, different logged-in user (distinct from auth_client's user)."""
    await client.post(
        "/auth/register",
        json={
            "email": "second@example.com",
            "full_name": "Second User",
            "password": "password123",
        },
    )
    resp = await client.post(
        "/auth/login",
        data={"username": "second@example.com", "password": "password123"},
    )
    token = resp.json()["access_token"]
    # NOTE: this shares the same underlying `client`, so we can't have both users
    # authenticated on it at once. This fixture is used in a dedicated test that
    # creates the project as user A, then switches the header to user B.
    return token


async def test_cannot_access_others_project(client):
    # ---- User A registers, logs in, creates a project. ----
    await client.post(
        "/auth/register",
        json={
            "email": "usera@example.com",
            "full_name": "A",
            "password": "password123",
        },
    )
    a_login = await client.post(
        "/auth/login",
        data={"username": "usera@example.com", "password": "password123"},
    )
    a_token = a_login.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {a_token}"
    pid = (await client.post("/projects", json={"name": "A's secret"})).json()["id"]

    # ---- User B registers, logs in, tries to touch A's project. ----
    await client.post(
        "/auth/register",
        json={
            "email": "userb@example.com",
            "full_name": "B",
            "password": "password123",
        },
    )
    b_login = await client.post(
        "/auth/login",
        data={"username": "userb@example.com", "password": "password123"},
    )
    b_token = b_login.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {b_token}"

    # B reads A's project -> 403 Permission Denied.
    resp = await client.get(f"/projects/{pid}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "permission_denied"

    # B updates A's project -> 403.
    resp = await client.patch(f"/projects/{pid}", json={"name": "hijacked"})
    assert resp.status_code == 403

    # B's own project list must be empty (isolation).
    resp = await client.get("/projects")
    assert resp.json() == []

"""
test_auth.py — registration, login, and the auth guard.

These tests cover both the HAPPY PATH (valid input succeeds) and the ERROR PATHS
(bad input / wrong credentials fail with the right status). Testing failure
modes is just as important as success — it's where real bugs hide, and
interviewers look for it.
"""


async def test_register_success(client):
    resp = await client.post(
        "/auth/register",
        json={
            "email": "new@example.com",
            "full_name": "New Person",
            "password": "password123",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    # The response must include public fields...
    assert body["email"] == "new@example.com"
    assert body["is_active"] is True
    # ...and must NOT leak the password or its hash. This asserts our response
    # model actually strips secrets — a security regression test.
    assert "password" not in body
    assert "hashed_password" not in body


async def test_register_duplicate_email_conflict(client):
    payload = {
        "email": "dupe@example.com",
        "full_name": "Dupe",
        "password": "password123",
    }
    first = await client.post("/auth/register", json=payload)
    assert first.status_code == 201

    # Second registration with the same email must be rejected with 409 Conflict
    # and our standard error envelope.
    second = await client.post("/auth/register", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


async def test_register_invalid_email_422(client):
    # "not-an-email" fails EmailStr validation -> 422 before our code runs.
    resp = await client.post(
        "/auth/register",
        json={
            "email": "not-an-email",
            "full_name": "X",
            "password": "password123",
        },
    )
    assert resp.status_code == 422
    # And even validation errors use our envelope.
    assert resp.json()["error"]["code"] == "validation_error"


async def test_register_short_password_422(client):
    resp = await client.post(
        "/auth/register",
        json={"email": "a@b.com", "full_name": "A", "password": "short"},
    )
    assert resp.status_code == 422


async def test_login_success_returns_token(client):
    await client.post(
        "/auth/register",
        json={
            "email": "login@example.com",
            "full_name": "Login User",
            "password": "password123",
        },
    )
    resp = await client.post(
        "/auth/login",
        data={"username": "login@example.com", "password": "password123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 0


async def test_login_wrong_password_401(client):
    await client.post(
        "/auth/register",
        json={
            "email": "wrong@example.com",
            "full_name": "W",
            "password": "password123",
        },
    )
    resp = await client.post(
        "/auth/login",
        data={"username": "wrong@example.com", "password": "WRONGpassword"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_protected_route_requires_token(client):
    # No Authorization header -> 401. Confirms the auth dependency guards routes.
    resp = await client.get("/users/me")
    assert resp.status_code == 401


async def test_me_returns_current_user(auth_client):
    # auth_client is already logged in (see conftest). /users/me should return
    # THAT user, proving the token was decoded and the user loaded.
    resp = await auth_client.get("/users/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == "tester@example.com"


async def test_invalid_token_rejected(client):
    # A garbage token must be rejected (signature verification fails) -> 401.
    client.headers["Authorization"] = "Bearer this.is.not.a.valid.jwt"
    resp = await client.get("/users/me")
    assert resp.status_code == 401

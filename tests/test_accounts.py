"""Accounts, sessions, and the boundary between one user's data and another's."""

from __future__ import annotations

import pytest

from backend import accounts

GOOD = {"email": "ana@example.com", "password": "correct horse battery"}


def _register(client, **overrides):
    return client.post("/api/auth/register", json={**GOOD, **overrides})


# ------------------------------------------------------------------ hashing
def test_a_password_is_never_stored_in_the_clear():
    stored = accounts.hash_password("correct horse battery")
    assert "correct horse battery" not in stored
    assert stored.startswith("pbkdf2$")
    assert accounts.verify_password("correct horse battery", stored)
    assert not accounts.verify_password("Correct horse battery", stored)


def test_the_same_password_hashes_differently_every_time():
    """A per-user salt, so identical passwords are not identifiable as such."""
    first = accounts.hash_password("same password")
    second = accounts.hash_password("same password")
    assert first != second
    assert accounts.verify_password("same password", first)
    assert accounts.verify_password("same password", second)


@pytest.mark.parametrize("stored", ["", "garbage", "pbkdf2$only$two", "md5$1$aa$bb"])
def test_a_malformed_hash_never_verifies(stored):
    assert not accounts.verify_password("anything", stored)


# ----------------------------------------------------------- registration
def test_register_signs_you_in(client):
    response = _register(client)
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "ana@example.com"
    assert body["max_learners"] == accounts.MAX_LEARNERS_PER_USER
    assert body["max_paths_per_learner"] == 3
    assert client.get("/api/auth/me").json()["signed_in"] is True


@pytest.mark.parametrize("email", ["", "ana", "ana@", "@example.com", "a b@c.com"])
def test_a_bad_email_is_refused(client, email):
    assert _register(client, email=email).status_code == 400


@pytest.mark.parametrize("password", ["", "short", "1234567"])
def test_a_weak_password_is_refused(client, password):
    response = _register(client, password=password)
    assert response.status_code == 400
    assert "8 characters" in response.json()["detail"]


def test_the_same_email_cannot_register_twice(client):
    _register(client)
    client.post("/api/auth/logout")
    again = _register(client)
    assert again.status_code == 400
    assert "already exists" in again.json()["detail"]


def test_email_is_case_and_space_insensitive(client):
    _register(client)
    client.post("/api/auth/logout")
    signed_in = client.post("/api/auth/login", json={
        "email": "  ANA@Example.COM  ", "password": GOOD["password"],
    })
    assert signed_in.status_code == 200


# ------------------------------------------------------------------ login
def test_login_and_logout(client):
    _register(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["signed_in"] is False

    assert client.post("/api/auth/login", json=GOOD).status_code == 200
    assert client.get("/api/auth/me").json()["signed_in"] is True

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").json()["signed_in"] is False


@pytest.mark.parametrize("attempt", [
    {"email": "ana@example.com", "password": "wrong password entirely"},
    {"email": "nobody@example.com", "password": "correct horse battery"},
])
def test_bad_credentials_are_refused_identically(client, attempt):
    _register(client)
    client.post("/api/auth/logout")
    response = client.post("/api/auth/login", json=attempt)
    assert response.status_code == 401
    # The same message either way: no hint about which accounts exist.
    assert response.json()["detail"] == "email or password is incorrect"


def test_the_session_cookie_is_not_readable_by_scripts(client):
    response = _register(client)
    cookie = response.headers.get("set-cookie", "").lower()
    assert accounts.SESSION_COOKIE in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_a_forged_session_token_is_ignored(client):
    _register(client)
    client.cookies.set(accounts.SESSION_COOKIE, "not-a-real-token")
    assert client.get("/api/auth/me").json()["signed_in"] is False


# --------------------------------------------------------------- guests
def test_a_guest_can_use_the_product_without_signing_up(client):
    response = client.post("/api/auth/guest")
    assert response.status_code == 201
    assert response.json()["is_guest"] is True
    assert client.get("/api/auth/me").json()["signed_in"] is True

    created = client.post("/api/learners", json={"name": "Trial"})
    assert created.status_code == 201
    assert [p["name"] for p in client.get("/api/learners").json()] == ["Trial"]


# ------------------------------------------------------------- separation
def test_one_user_never_sees_another_users_learners(client):
    _register(client, email="first@example.com")
    client.post("/api/learners", json={"name": "Belongs to first"})
    client.post("/api/auth/logout")

    _register(client, email="second@example.com")
    assert client.get("/api/learners").json() == []

    client.post("/api/learners", json={"name": "Belongs to second"})
    assert [p["name"] for p in client.get("/api/learners").json()] == [
        "Belongs to second"
    ]


def test_another_users_learner_is_not_reachable_by_id(client):
    _register(client, email="first@example.com")
    theirs = client.post("/api/learners", json={"name": "Private"}).json()["learner_id"]
    client.post("/api/auth/logout")

    _register(client, email="second@example.com")
    # 404, not 403 — the caller learns nothing about whether that id exists.
    assert client.get(f"/api/learners/{theirs}/profile").status_code == 404
    assert client.get(f"/api/learners/{theirs}/dashboard").status_code == 404
    assert client.delete(f"/api/learners/{theirs}").status_code == 404


def test_signing_up_adopts_learners_made_before_anyone_signed_in(client):
    """Nothing created during a logged-out session is orphaned."""
    client.post("/api/learners", json={"name": "Made before accounts"})
    _register(client)
    assert [p["name"] for p in client.get("/api/learners").json()] == [
        "Made before accounts"
    ]


# ------------------------------------------------------------------ limits
def test_a_user_can_keep_five_learners(client):
    _register(client)
    for index in range(accounts.MAX_LEARNERS_PER_USER):
        assert client.post(
            "/api/learners", json={"name": f"Learner {index + 1}"}
        ).status_code == 201

    refused = client.post("/api/learners", json={"name": "One too many"})
    assert refused.status_code == 400
    assert str(accounts.MAX_LEARNERS_PER_USER) in refused.json()["detail"]


def test_deleting_a_learner_frees_a_slot(client):
    _register(client)
    ids = [
        client.post("/api/learners", json={"name": f"L{i}"}).json()["learner_id"]
        for i in range(accounts.MAX_LEARNERS_PER_USER)
    ]
    assert client.post("/api/learners", json={}).status_code == 400

    client.delete(f"/api/learners/{ids[0]}")
    assert client.post("/api/learners", json={"name": "Replacement"}).status_code == 201


def test_the_cap_is_per_user_not_global(client):
    _register(client, email="first@example.com")
    for i in range(accounts.MAX_LEARNERS_PER_USER):
        client.post("/api/learners", json={"name": f"A{i}"})
    client.post("/api/auth/logout")

    _register(client, email="second@example.com")
    assert client.post("/api/learners", json={"name": "Theirs"}).status_code == 201


# ------------------------------------------------------------- passwords
def test_a_user_can_change_their_password(client):
    _register(client)
    assert client.patch("/api/auth/password", json={
        "current": GOOD["password"], "new": "a whole new password",
    }).status_code == 204

    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json=GOOD).status_code == 401
    assert client.post("/api/auth/login", json={
        "email": GOOD["email"], "password": "a whole new password",
    }).status_code == 200


def test_changing_a_password_needs_the_current_one(client):
    _register(client)
    response = client.patch("/api/auth/password", json={
        "current": "not it", "new": "a whole new password",
    })
    assert response.status_code == 400
    assert "current password" in response.json()["detail"]


def test_changing_a_password_requires_signing_in(client):
    assert client.patch("/api/auth/password", json={
        "current": "x", "new": "a whole new password",
    }).status_code == 401


# ------------------------------------------------------ guest becomes real
def test_a_guest_keeps_everything_when_they_create_an_account(client):
    """The whole point of guest mode: trying it first must not cost you the work."""
    client.post("/api/auth/guest")
    learner = client.post("/api/learners", json={"name": "Built as a guest"}).json()
    client.patch(f"/api/learners/{learner['learner_id']}/profile",
                 json={"goal_id": "goal-data-analyst"})

    upgraded = client.post("/api/auth/upgrade", json={
        "email": "real@example.com", "password": "a real password",
    })
    assert upgraded.status_code == 200
    assert upgraded.json()["is_guest"] is False

    # Same learner, same path, still signed in.
    assert [p["name"] for p in client.get("/api/learners").json()] == ["Built as a guest"]
    assert client.get(
        f"/api/learners/{learner['learner_id']}/path"
    ).json()["goal_id"] == "goal-data-analyst"

    # And the new credentials work after signing out.
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={
        "email": "real@example.com", "password": "a real password",
    }).status_code == 200
    assert [p["name"] for p in client.get("/api/learners").json()] == ["Built as a guest"]


def test_a_registered_account_cannot_be_upgraded_again(client):
    _register(client)
    response = client.post("/api/auth/upgrade", json={
        "email": "other@example.com", "password": "another password",
    })
    assert response.status_code == 400
    assert "already registered" in response.json()["detail"]


def test_upgrading_to_a_taken_email_is_refused(client):
    _register(client, email="taken@example.com")
    client.post("/api/auth/logout")
    client.post("/api/auth/guest")

    response = client.post("/api/auth/upgrade", json={
        "email": "taken@example.com", "password": "a real password",
    })
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]

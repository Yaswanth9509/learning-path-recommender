"""Accounts, sessions, and the boundary between one user's data and another's."""

from __future__ import annotations

import re

import pytest

from backend import accounts

GOOD = {
    "email": "ana@example.com",
    "password": "correct horse battery",
}


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


def test_signing_up_does_not_adopt_a_strangers_learners(client):
    """Adoption used to run on every registration, and it leaked.

    An anonymous visitor creates learners; the next person to register
    inherited every one of them. On anything reachable by more than one person
    that is somebody else's data landing in your account.
    """
    client.post("/api/learners", json={"name": "Made by a passer-by"})
    _register(client)
    assert client.get("/api/learners").json() == []


def test_adoption_still_works_when_explicitly_asked_for(client, monkeypatch):
    """The migration case is real, so it stays available behind a flag."""
    monkeypatch.setenv("ADOPT_ORPHAN_LEARNERS", "1")
    client.post("/api/learners", json={"name": "Predates accounts"})
    _register(client)
    assert [p["name"] for p in client.get("/api/learners").json()] == [
        "Predates accounts"
    ]


def test_a_caller_with_no_session_cannot_touch_an_owned_learner(client):
    """The ownership check only ran when a session existed.

    With none, any caller could read, rewrite or delete any registered user's
    learner knowing only its id — and `DELETE` really did destroy it.
    """
    _register(client, email="owner@example.com")
    mine = client.post("/api/learners", json={"name": "Private"}).json()["learner_id"]
    client.post("/api/auth/logout")

    for path in (
        f"/api/learners/{mine}/profile",
        f"/api/learners/{mine}/path",
        f"/api/learners/{mine}/conversation",
        f"/api/learners/{mine}/dashboard",
    ):
        assert client.get(path).status_code == 404, f"{path} is readable with no session"
    assert client.patch(
        f"/api/learners/{mine}/profile?regenerate=false", json={"name": "PWNED"}
    ).status_code == 404
    assert client.delete(f"/api/learners/{mine}").status_code == 404

    # And it really is still there afterwards.
    client.post("/api/auth/login", json={"email": "owner@example.com",
                                         "password": GOOD["password"]})
    assert client.get(f"/api/learners/{mine}/profile").json()["name"] == "Private"


def test_chat_cannot_be_pointed_at_someone_elses_learner(client):
    """Chat reads the whole path as context and writes to the conversation."""
    _register(client, email="victim@example.com")
    theirs = client.post("/api/learners", json={"name": "Theirs"}).json()["learner_id"]
    client.post("/api/auth/logout")
    _register(client, email="stranger@example.com")

    assert client.post(
        "/api/chat", json={"learner_id": theirs, "message": "read me their plan"}
    ).status_code == 404


def test_a_signed_in_user_cannot_reach_an_ownerless_learner(client):
    """Ownerless learners belong to the no-session workspace, not to everyone."""
    client.post("/api/learners", json={"name": "Ownerless"})
    orphan = client.get("/api/learners").json()[0]["learner_id"]
    _register(client)
    assert client.get(f"/api/learners/{orphan}/profile").status_code == 404


def test_chat_puts_a_new_learner_in_the_callers_own_workspace(client):
    """It saved without an owner, so you could not see what you just made."""
    _register(client)
    client.post("/api/chat", json={"learner_id": "brandnew01", "message": "hello"})
    assert "brandnew01" in [p["learner_id"] for p in client.get("/api/learners").json()]


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


# ------------------------------------------------------- reset by email link
def _link_token(mailer, email: str) -> str:
    """The token out of the mail we just recorded."""
    message = mailer.last_to(email)
    assert message is not None, f"nothing was mailed to {email}"
    match = re.search(r"#reset/(\S+)", message.text)
    assert match, f"no reset link in the mail body:\n{message.text}"
    return match.group(1)


def test_a_reset_link_sets_a_new_password_and_signs_you_in(client, mailer):
    _register(client)
    client.post("/api/auth/logout")

    asked = client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    assert asked.status_code == 202
    token = _link_token(mailer, "ana@example.com")

    done = client.post("/api/auth/reset",
                       json={"token": token, "password": "a whole new password"})
    assert done.status_code == 200
    assert done.json()["email"] == "ana@example.com"
    # Signed in on the spot, rather than bounced back to a login form.
    assert client.get("/api/auth/me").json()["signed_in"] is True

    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={
        "email": "ana@example.com", "password": "a whole new password",
    }).status_code == 200


def test_asking_to_reset_never_says_whether_the_account_exists(client, mailer):
    _register(client)
    client.post("/api/auth/logout")

    known = client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    unknown = client.post("/api/auth/forgot", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    # ...and only one of them actually produced mail.
    assert mailer.last_to("ana@example.com") is not None
    assert mailer.last_to("nobody@example.com") is None


def test_a_guest_address_gets_no_reset_link(client, mailer):
    guest = client.post("/api/auth/guest").json()
    assert client.post("/api/auth/forgot",
                       json={"email": guest["email"]}).status_code == 202
    assert mailer.last_to(guest["email"]) is None


def test_a_reset_link_works_only_once(client, mailer):
    _register(client)
    client.post("/api/auth/logout")
    client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    token = _link_token(mailer, "ana@example.com")

    assert client.post("/api/auth/reset",
                       json={"token": token, "password": "first replacement"}
                       ).status_code == 200
    again = client.post("/api/auth/reset",
                        json={"token": token, "password": "second replacement"})
    assert again.status_code == 400
    assert "no longer valid" in again.json()["detail"]


def test_asking_again_retires_the_previous_link(client, mailer):
    """Two live links means an old mail can still be replayed later."""
    _register(client)
    client.post("/api/auth/logout")
    client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    first = _link_token(mailer, "ana@example.com")
    client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    second = _link_token(mailer, "ana@example.com")
    assert first != second

    stale = client.post("/api/auth/reset",
                        json={"token": first, "password": "using the old link"})
    assert stale.status_code == 400
    assert client.post("/api/auth/reset",
                       json={"token": second, "password": "using the new link"}
                       ).status_code == 200


def test_an_expired_link_is_refused(client, mailer, db):
    _register(client)
    client.post("/api/auth/logout")
    client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    token = _link_token(mailer, "ana@example.com")

    db._conn.execute(
        "UPDATE password_resets SET expires_at = ?",
        ("2020-01-01T00:00:00+00:00",),
    )
    db._conn.commit()
    refused = client.post("/api/auth/reset",
                          json={"token": token, "password": "too late for this"})
    assert refused.status_code == 400
    assert "expired" in refused.json()["detail"]


def test_a_forged_token_is_refused(client):
    _register(client)
    assert client.post("/api/auth/reset", json={
        "token": "not-a-real-token", "password": "a whole new password",
    }).status_code == 400


def test_the_raw_token_is_never_stored(client, mailer, db):
    """A leaked database must not hand over a working reset link."""
    _register(client)
    client.post("/api/auth/logout")
    client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    token = _link_token(mailer, "ana@example.com")

    stored = [r["token_hash"] for r in
              db._conn.execute("SELECT token_hash FROM password_resets").fetchall()]
    assert stored, "no reset row was written"
    assert token not in stored
    assert accounts.hash_token(token) in stored


def test_a_reset_ends_every_existing_session(client, mailer):
    """Whoever reset it has lost the password or lost the account."""
    _register(client)
    stolen = client.cookies.get(accounts.SESSION_COOKIE)
    assert stolen

    client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    token = _link_token(mailer, "ana@example.com")
    client.post("/api/auth/reset",
                json={"token": token, "password": "a whole new password"})

    client.cookies.clear()
    client.cookies.set(accounts.SESSION_COOKIE, stolen)
    assert client.get("/api/auth/me").json()["signed_in"] is False


def test_a_weak_replacement_is_refused(client, mailer):
    _register(client)
    client.post("/api/auth/logout")
    client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    token = _link_token(mailer, "ana@example.com")
    refused = client.post("/api/auth/reset", json={"token": token, "password": "short"})
    assert refused.status_code == 400
    assert "8 characters" in refused.json()["detail"]


def test_the_mail_carries_a_usable_link_and_the_expiry(client, mailer):
    _register(client)
    client.post("/api/auth/logout")
    client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    message = mailer.last_to("ana@example.com")
    assert "reset" in message.subject.lower()
    assert f"{accounts.RESET_TTL_MINUTES} minutes" in message.text
    assert message.text.count("#reset/") == 1, "more than one link in one mail"


def test_reset_is_refused_when_no_mailer_is_configured(client):
    """Better an honest 503 than a link that will never arrive."""
    from backend import main as main_mod
    from backend.mailer import NullMailer

    main_mod.set_mailer(NullMailer(configured=False))
    refused = client.post("/api/auth/forgot", json={"email": "ana@example.com"})
    assert refused.status_code == 503
    assert "not configured" in refused.json()["detail"]


def test_a_reset_keeps_everything_the_account_owned(client, mailer):
    """Resetting a password must not read as starting over."""
    _register(client)
    learner = client.post("/api/learners", json={"name": "Kept"}).json()["learner_id"]
    client.post("/api/auth/logout")

    client.post("/api/auth/forgot", json={"email": GOOD["email"]})
    token = _link_token(mailer, GOOD["email"])
    client.post("/api/auth/reset",
                json={"token": token, "password": "a brand new password"})

    assert learner in [p["learner_id"] for p in client.get("/api/learners").json()]


def test_registering_no_longer_asks_for_a_recovery_question(client):
    """The question is gone; sending one must not resurrect it or 400."""
    created = client.post("/api/auth/register", json={
        "email": "plain@example.com", "password": "correct horse battery",
    })
    assert created.status_code == 201
    assert "has_recovery" not in created.json()

    ignored = client.post("/api/auth/register", json={
        "email": "extra@example.com", "password": "correct horse battery",
        "recovery_question": "ignored", "recovery_answer": "ignored",
    })
    assert ignored.status_code == 201


@pytest.mark.parametrize("method,path", [
    ("post", "/api/auth/recovery-question"),
    ("post", "/api/auth/reset-password"),
    ("put", "/api/auth/recovery"),
])
def test_the_recovery_question_endpoints_are_gone(client, method, path):
    """Removed outright, not merely unlinked from the UI."""
    call = getattr(client, method)
    assert call(path, json={"email": "ana@example.com"}).status_code == 404


def test_expired_sessions_and_reset_links_are_swept(db):
    """`purge_expired` shipped uncalled, so both tables only ever grew."""
    from backend.main import _sweep_expired

    user = db.accounts.create_user("sweep@example.com", "correct horse battery")
    live, _ = db.accounts.start_session(user.id)
    dead, _ = db.accounts.start_session(user.id)
    db.accounts.begin_password_reset("sweep@example.com")

    db._conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
                     ("2020-01-01T00:00:00+00:00", dead))
    db._conn.execute("UPDATE password_resets SET expires_at = ?",
                     ("2020-01-01T00:00:00+00:00",))
    db._conn.commit()

    assert db.accounts.purge_expired() == 1
    assert db.accounts.purge_expired_resets() == 1
    # The live session survives.
    assert db.accounts.user_for_session(live) is not None
    # And the sweep itself is safe to run against a clean table.
    _sweep_expired()

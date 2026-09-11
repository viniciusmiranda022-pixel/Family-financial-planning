"""Shared test helper to drive a `TestClient` through mandatory local MFA
enrollment (docs/WORK_ORDER_LOCAL_MFA_TOTP.md).

Every test module across this suite that authenticates an HTTP client --
via `POST /api/auth/setup` or `POST /api/auth/login` -- now gets back
`{"mfa_required": true, "mode": "enroll"|"verify"}` instead of an
immediate `ffp_session`, since no active user (including the very first
admin created by `/auth/setup`) can reach a full session before completing
TOTP. `complete_mfa_enrollment` finishes that flow the same way a real
Google Authenticator user would -- scan the QR's secret, compute the
current code -- just computed locally with `pyotp` instead of a phone, so
every pre-existing fixture that only cared about "this client now has a
full session" keeps working with one extra call after setup/login.

Deliberately not a fixture that also re-implements login itself: each test
module's own `_setup_household`/`_client` helper already owns exactly how
it authenticates (some via `/auth/setup`, some via a direct DB-seeded user
plus `/auth/login`, matching `tests/test_role_based_authorization.py`'s
own equivalent `_complete_mfa_enrollment`). This module only owns the part
that is genuinely identical everywhere: finishing the enrollment challenge
once a client is sitting in `MFA_ENROLL_PENDING`.
"""

import pyotp
from fastapi.testclient import TestClient


def complete_mfa_enrollment(client: TestClient) -> str:
    """Complete TOTP enrollment for a client currently in
    `MFA_ENROLL_PENDING` (i.e. that just called `/auth/setup` or
    `/auth/login` for a user with no confirmed factor yet). Leaves the
    client holding a full `ffp_session` cookie.

    Returns the plaintext TOTP secret -- callers that need to log this same
    user in again later in the same test (a second `TestClient`, or after
    `/auth/logout`) keep it and pass it to `complete_mfa_verification`.
    Callers that only need the confirmation response itself (e.g. the
    recovery codes) can still reach it via `client.post("/api/auth/mfa/enroll/confirm", ...)`
    directly instead of this helper.
    """

    start = client.post("/api/auth/mfa/enroll/start")
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirm = client.post("/api/auth/mfa/enroll/confirm", json={"code": code})
    assert confirm.status_code == 200, confirm.text
    return secret


def complete_mfa_verification(client: TestClient, secret: str) -> dict:
    """Complete a TOTP challenge for a client currently in
    `MFA_VERIFY_PENDING` (a user who already has a confirmed factor --
    from an earlier `complete_mfa_enrollment` -- and just called
    `/auth/login` again). Needs the plaintext secret returned by that
    earlier enrollment's `/auth/mfa/enroll/start` call.
    """

    code = pyotp.TOTP(secret).now()
    verify = client.post("/api/auth/mfa/verify", json={"code": code})
    assert verify.status_code == 200, verify.text
    return verify.json()

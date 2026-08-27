# Tailnet identity on write routes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the write UI live on the normal published URL — no SSH tunnel, one service — by having the app verify the caller's tailnet identity, so the network ACL stops being the only thing between a shared tailnet and an unauthenticated write API.

**Architecture:** The reverse proxy in front of this app injects an identity header naming the authenticated tailnet user. A FastAPI dependency on the write routes checks that header against an allowlist held in gitignored config. Reads are unchanged and remain unauthenticated — the ACL gates those, and it always did.

**Tech Stack:** Python 3.12, FastAPI, pytest (asyncio auto mode).

**Spec:** `docs/superpowers/specs/2026-08-24-entry-import-design.md` §6 — which currently mandates the *opposite* arrangement and is amended by Task 2 rather than left to rot.

## Why this replaces the previous arrangement

§6 kept write routes off the published instance entirely, on the reasoning that the tailnet is shared and the ACL was unverified from this host. The owner has since shown the ACL: it is **default-deny**, scoped per-node-per-port, and grants the admin group `*:*` while no other group has any entry for the node in question. So a published port is admin-only automatically, with no rule to remember — the fail-closed property §6 wanted, obtained a different way.

The cost of the old arrangement was a persistent SSH tunnel for every manual fill or import, and a control that is too annoying to use is a control that ends up unused.

This plan does not simply trust the ACL, though. The app has no auth of its own, so the ACL would otherwise be the *only* layer; a single widening edit would expose an unauthenticated write path into a financial ledger. The identity check makes that a two-layer failure instead of a one-layer one.

**Verified before this plan was written**, not assumed: a request through the proxy arrives carrying `Tailscale-User-Login`, `Tailscale-User-Name`, `Tailscale-User-Profile-Pic`, and `X-Forwarded-For`. The proxy sets these on ingress, so a caller can neither strip them nor forge them.

## Global Constraints

- **`request.client.host` and `X-Forwarded-For` must NEVER be used as access control.** This is unchanged and non-negotiable. The proxy is the client, so the source address is meaningless here; `X-Forwarded-For` is caller-influenced in the general case. Identity comes from the identity header and nothing else. A test pins this.
- **Fail closed.** If the allowlist is unset or empty, writes are REFUSED — never allowed. This is deliberately the opposite of `DEADBAND_ENABLE_WRITES`, whose `bool(os.environ.get(...))` treats `=0` as enabled (recorded as gap #61). A missing allowlist must never read as "permit everyone".
- **The allowlist lives in gitignored config, never in the repo.** The values are personal email addresses and this repository is PUBLIC. They belong in the deployment's env file. No real login may appear in any tracked file — including tests, which use invented addresses.
- Handlers return `DeadbandJSONResponse`, never a bare dict.
- DB tests run foreground, summary line read: `set -a && . ./.env && set +a && uv run pytest <paths>`. Never pipe to `tail`.
- Comments explain WHY.

---

### Task 1: The identity dependency

**Files:**
- Create: `api/identity.py`
- Test: `tests/api/test_identity.py`

**Interfaces:**
- Produces: `api/identity.py:require_trusted_identity(request) -> str` (a FastAPI dependency returning the caller's login), and `_TRUSTED_LOGINS_ENV = "DEADBAND_TRUSTED_LOGINS"`

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_identity.py
"""Tailnet identity on write routes.

Every login here is INVENTED. The real allowlist lives in the deployment's
gitignored env file -- the values are personal email addresses and this
repository is public.
"""

import pytest
from fastapi import HTTPException

from api.identity import require_trusted_identity


class _Req:
    """Minimal stand-in for a Request: the dependency reads headers and
    nothing else, and that narrowness is the point."""

    def __init__(self, **headers):
        self.headers = {k.replace("_", "-").lower(): v for k, v in headers.items()}


def _check(monkeypatch, allowlist, **headers):
    if allowlist is None:
        monkeypatch.delenv("DEADBAND_TRUSTED_LOGINS", raising=False)
    else:
        monkeypatch.setenv("DEADBAND_TRUSTED_LOGINS", allowlist)
    return require_trusted_identity(_Req(**headers))


def test_a_login_on_the_allowlist_is_accepted(monkeypatch):
    got = _check(monkeypatch, "alice@example.invalid", Tailscale_User_Login="alice@example.invalid")
    assert got == "alice@example.invalid"


def test_a_login_not_on_the_allowlist_is_refused(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, "alice@example.invalid", Tailscale_User_Login="mallory@example.invalid")
    assert exc.value.status_code == 403


def test_a_missing_identity_header_is_refused(monkeypatch):
    """No header means the request did not come through the proxy -- it
    reached the app's local port directly. Deny it."""
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, "alice@example.invalid")
    assert exc.value.status_code == 403


def test_an_unset_allowlist_refuses_rather_than_permits(monkeypatch):
    """The whole point. DEADBAND_ENABLE_WRITES reads '=0' as enabled (gap #61);
    this must not repeat that shape. Absent config is a refusal, and a
    DISTINCT one, so an operator can tell misconfiguration from denial."""
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, None, Tailscale_User_Login="alice@example.invalid")
    assert exc.value.status_code == 503


def test_an_empty_allowlist_refuses(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, "   ", Tailscale_User_Login="alice@example.invalid")
    assert exc.value.status_code == 503


def test_the_allowlist_tolerates_whitespace_and_case(monkeypatch):
    got = _check(
        monkeypatch,
        " Alice@Example.Invalid , bob@example.invalid ",
        Tailscale_User_Login="alice@example.invalid",
    )
    assert got == "alice@example.invalid"


def test_x_forwarded_for_is_never_consulted(monkeypatch):
    """Pins the prohibition. A caller-supplied source address must not be able
    to stand in for identity, no matter how plausible it looks."""
    with pytest.raises(HTTPException) as exc:
        _check(
            monkeypatch,
            "alice@example.invalid",
            X_Forwarded_For="203.0.113.1",
            Tailscale_User_Name="Alice",
        )
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.identity'`

- [ ] **Step 3: Implement**

Create `api/identity.py`. `require_trusted_identity` reads `DEADBAND_TRUSTED_LOGINS` at CALL time (not import time, so tests and a restart-free config change both work), splits on commas, strips and lower-cases each entry, and compares against the lower-cased `Tailscale-User-Login` header.

- Allowlist unset or empty after stripping → `HTTPException(503, ...)`, naming it as a configuration problem.
- Header absent → `HTTPException(403, ...)`.
- Header present but not on the list → `HTTPException(403, ...)`.
- Otherwise return the login.

The module docstring must say WHY: the proxy sets the identity header on ingress so a caller can neither strip nor forge it; `X-Forwarded-For` and `request.client.host` are present but are NOT identity and must never be consulted.

Do not log the login on the success path — it is a personal email address and the journal is less protected than the env file.

- [ ] **Step 4: Run to verify pass**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_identity.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add api/identity.py tests/api/test_identity.py
git commit -m "feat(api): verify tailnet identity, failing closed

The proxy injects an authenticated login the caller can neither strip nor
forge. An unset allowlist REFUSES rather than permits -- deliberately unlike
DEADBAND_ENABLE_WRITES, whose bool(getenv) reads '=0' as enabled (gap #61).
X-Forwarded-For arrives too and is never consulted; a test pins that."
```

---

### Task 2: Apply it to the write routes, and amend the spec

**Files:**
- Modify: `api/fills.py`, `api/imports.py`, `tests/api/conftest.py`
- Modify: `docs/superpowers/specs/2026-08-24-entry-import-design.md` (§6)
- Test: `tests/api/test_write_identity.py`

**Interfaces:**
- Consumes: `api/identity.py:require_trusted_identity`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_write_identity.py
"""Every write route requires tailnet identity; no read route does.

Walks the routes rather than listing them, so a write endpoint added later
without the dependency fails here instead of shipping unauthenticated.
All logins invented.
"""

from fastapi.routing import APIRoute, iter_route_contexts

from api.app import create_app
from api.identity import require_trusted_identity
from tests.conftest import requires_db

pytestmark = requires_db

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The read-only POST exception, kept in step with tests/api/test_write_pool.py.
_READ_ONLY_POST_PATHS = {"/api/imports/preview"}


def test_every_write_route_requires_identity():
    app = create_app(enable_writes=True)
    for rc in iter_route_contexts(app.routes):
        if not isinstance(rc.original_route, APIRoute) or not rc.path.startswith("/api/"):
            continue
        deps = {d.call.__name__ for d in rc.dependant.dependencies if d.call is not None}
        writes = bool(rc.methods & _WRITE_METHODS) and rc.path not in _READ_ONLY_POST_PATHS
        if writes:
            assert require_trusted_identity.__name__ in deps, (
                f"{sorted(rc.methods)} {rc.path} writes without an identity check"
            )
```

Note `/api/imports/preview` is exempt because it writes nothing — matching the existing exemption in `tests/api/test_write_pool.py`. Keep the two lists in step; if you can share one definition cleanly, do.

- [ ] **Step 2: Run to verify failure**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_write_identity.py -v`
Expected: FAIL — the write routes have no identity dependency yet.

- [ ] **Step 3: Add the dependency to every write route**

`POST /api/fills`, `DELETE /api/fills/{id}`, and `POST /api/imports/commit` each declare `Depends(require_trusted_identity)`. `POST /api/imports/preview` does not.

Then make the API test fixture supply a trusted identity, so the existing 51 tests keep passing: set `DEADBAND_TRUSTED_LOGINS` to an invented login for the test session and have the client send that header by default.

- [ ] **Step 4: Run the whole API suite**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api -q`
Expected: PASS, 0 skipped. Foreground; about two minutes.

- [ ] **Step 5: Amend spec §6**

§6 currently mandates that write routes are not registered on the published instance and describes a second unit reached by tunnel. Rewrite it to describe what is now true, keeping these points intact because they are still correct and still load-bearing:

- `request.client.host` must never be used as access control, and WHY (the proxy is the client, so it reads as localhost for every remote caller).
- `X-Forwarded-For` arrives and is likewise not identity.
- What replaced the old arrangement, and why the change was safe: the ACL is default-deny and per-port, so a published port is admin-only by construction; and the app now verifies identity itself, so the ACL is not the only layer.
- The fail-closed rule for the allowlist, and that its values never enter this repository.

Keep it a description of the current design, not a changelog. Do NOT name hosts, ports, IPs, tailnet names, or any real login — the repo is public and a pre-commit hook enforces a deny-list.

- [ ] **Step 6: Commit**

```bash
git add api/fills.py api/imports.py tests/api/ docs/superpowers/specs/2026-08-24-entry-import-design.md
git commit -m "feat(api): require tailnet identity on every write route

A route-walking test asserts it, so a write endpoint added later without the
dependency fails CI instead of shipping unauthenticated. Spec section 6 is
amended to describe the arrangement that now exists rather than the one it
replaced; the prohibition on source-address checks is unchanged."
```

---

### Task 3: Collapse to one service

**Files:**
- Modify: the deployment kit under gitignored `docs/ops/` — the service unit, the runbook, and the deploy script
- Delete: `docs/ops/deadband-write.service`, `docs/ops/install-write-instance.sh`

All of these are gitignored — this task changes the local deployment kit only and commits nothing but, possibly, documentation that is already tracked. Check before committing: if every file you touched is gitignored, say so and make no commit.

- [ ] **Step 1: One unit, writes enabled**

`docs/ops/deadband.service` gains `Environment=DEADBAND_ENABLE_WRITES=1` and a comment pointing at the identity check as what now guards writes. Its existing comment says the app has no auth code — that is no longer true for writes, so correct it.

The allowlist itself goes in `/home/swizz/deadband/deadband.env` (mode 600, already referenced by `EnvironmentFile=`) as `DEADBAND_TRUSTED_LOGINS=...`. **Do not put real logins in any file under `docs/ops/` that this task writes** — instead, the runbook instructs the operator to add the line themselves. Even gitignored, the kit gets copied around.

- [ ] **Step 2: Remove the second instance from the deploy script**

The deploy script under `docs/ops/` currently restarts and health-checks the second unit when present. Remove both, since that unit is going away. Keep the health check on the remaining service.

- [ ] **Step 3: Runbook**

Rewrite the write-instance section: one service, reachable on the normal published URL, no tunnel. Include the teardown commands the operator runs (disable and remove the old unit, reload), and the line to add to the env file. State the verification that actually proves it: a write path returns 403 without a trusted identity and succeeds with one.

- [ ] **Step 4: Delete the superseded kit files**

Remove `docs/ops/deadband-write.service` and `docs/ops/install-write-instance.sh`.

- [ ] **Step 5: Report the operator commands**

These need root on the deployment host and cannot be run from here. List them in your report for the controller to hand over: disabling and removing the old unit, adding the env line, restarting, and the two verification curls.

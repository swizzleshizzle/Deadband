"""Verify the caller's identity on write routes.

This app sits behind a reverse proxy that authenticates every caller and,
on ingress, injects a `Tailscale-User-Login` header naming the authenticated
user. Because the proxy sets that header rather than relaying one supplied
by the client, a caller can neither strip it nor forge it -- it is the one
signal on the request that is actually trustworthy identity.

`X-Forwarded-For` and `request.client.host` are NOT identity, even though
both are present on every request. The proxy is the client as far as this
process is concerned, so `request.client.host` reads as localhost for every
remote caller, and `X-Forwarded-For` is just a header the original client
could have set to anything before the proxy ever saw it. Consulting either
one would pass exactly the requests this dependency exists to stop, so
neither is read here -- anywhere.

`DEADBAND_TRUSTED_LOGINS` must fail closed: unset or empty means refuse,
never "permit everyone". This is deliberately the opposite of
`DEADBAND_ENABLE_WRITES` elsewhere in this codebase, whose `bool(os.environ
.get(...))` treats `=0` as enabled (known-gap #61) -- that shape must not be
repeated here.
"""

import os

from fastapi import HTTPException, Request

_TRUSTED_LOGINS_ENV = "DEADBAND_TRUSTED_LOGINS"

_IDENTITY_HEADER = "tailscale-user-login"


def require_trusted_identity(request: Request) -> str:
    """FastAPI dependency: return the caller's login, or refuse the request.

    Reads the allowlist from the environment on every call (not at import
    time) so tests can monkeypatch it and an operator can change it without
    restarting the process.
    """
    raw_allowlist = os.environ.get(_TRUSTED_LOGINS_ENV, "")
    allowlist = {
        entry.strip().lower()
        for entry in raw_allowlist.split(",")
        if entry.strip()
    }
    if not allowlist:
        # Distinct from a 403 on purpose: this is misconfiguration (nobody
        # is allowed in at all), not a denied caller, and an operator
        # troubleshooting "writes are down" needs to be able to tell those
        # apart.
        raise HTTPException(status_code=503, detail="Identity allowlist is not configured")

    login = request.headers.get(_IDENTITY_HEADER)
    if not login:
        raise HTTPException(status_code=403, detail="Missing identity header")

    if login.strip().lower() not in allowlist:
        raise HTTPException(status_code=403, detail="Caller is not on the trusted-identity allowlist")

    return login

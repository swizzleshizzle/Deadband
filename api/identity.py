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

This file does not simply trust that the proxy behaves as observed. Whether
the proxy REPLACES a client-sent identity header or APPENDS to one is not
pinned by anything in this repository, so if a caller could sneak in their
own copy and have the proxy's copy sort second, `Headers.get` would silently
return the attacker's value. Rather than depend on an assumption about
upstream behavior, this dependency reads ALL values for the header and
refuses outright if there is more than one -- a legitimate proxied request
only ever carries exactly one.

The returned login is normalized (stripped, lower-cased) rather than the raw
header value, because it becomes an actor string recorded on ledger writes.
Returning the raw value would let whitespace or case variation on an
otherwise-identical login split one person's audit trail into several
strings.

Non-ASCII login values are rejected before comparison. `str.lower()` folds
some non-ASCII characters onto ASCII ones (e.g. U+212A KELVIN SIGN folds to
"k"), which could let a lookalike glyph pass as a match. Real tailnet logins
are ASCII email addresses, so requiring ASCII costs nothing and closes that
class of confusion -- it is not reachable through the proxy today, but nothing
in this file should depend on that remaining true.
"""

import os

from fastapi import HTTPException, Request

_TRUSTED_LOGINS_ENV = "DEADBAND_TRUSTED_LOGINS"

_IDENTITY_HEADER = "tailscale-user-login"


def require_trusted_identity(request: Request) -> str:
    """FastAPI dependency: return the caller's normalized login, or refuse.

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

    # Exactly one value is the only shape a genuine proxied request can take.
    # Zero means the request did not come through the proxy; more than one
    # means either a caller supplied their own copy or the proxy appends
    # rather than replaces -- both are bypass attempts as far as this
    # dependency can tell, and neither is worth guessing about.
    logins = request.headers.getlist(_IDENTITY_HEADER)
    if len(logins) != 1:
        raise HTTPException(status_code=403, detail="Missing or duplicated identity header")

    login = logins[0]
    if not login or not login.isascii():
        raise HTTPException(status_code=403, detail="Identity header is not a valid login")

    normalized = login.strip().lower()
    if normalized not in allowlist:
        raise HTTPException(status_code=403, detail="Caller is not on the trusted-identity allowlist")

    return normalized

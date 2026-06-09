"""
auth.py — Optional HTTP Basic authentication for secure deployment.

If APP_PASSWORD is set, every page and API route requires HTTP Basic credentials
(username defaults to "admin", override with APP_USERNAME). If APP_PASSWORD is
unset/empty, auth is disabled and a warning is logged, so local development stays
frictionless — set the env var only on the deployed instance.

Wired in main.py as an app-wide dependency: FastAPI(dependencies=[Depends(require_auth)]).
Static assets are mounted as a sub-app and are intentionally left open.
"""
import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# auto_error=False: a missing header returns None here instead of FastAPI's own
# 401, so we can honor the auth-disabled path when APP_PASSWORD is unset.
_security = HTTPBasic(auto_error=False)

if not APP_PASSWORD:
    print("[auth] APP_PASSWORD not set — authentication DISABLED. "
          "Set APP_PASSWORD (and optionally APP_USERNAME) to protect this deployment.",
          flush=True)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_security)) -> None:
    """Enforce HTTP Basic auth when APP_PASSWORD is configured; no-op otherwise."""
    if not APP_PASSWORD:
        return  # auth disabled — open access for local/dev use

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Basic"},
    )
    if credentials is None:
        raise unauthorized

    # constant-time compare on both fields to avoid leaking length/timing
    user_ok = secrets.compare_digest(credentials.username, APP_USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not (user_ok and pass_ok):
        raise unauthorized

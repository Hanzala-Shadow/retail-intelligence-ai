"""Google Drive OAuth desktop flow — shared by both pipelines.

The same ~30 lines lived in three places before the split:
``esg/src/drive_downloader.py``, ``esg/src/count_drive_pdfs.py`` and
``filings/src/drive_uploader.py`` — whose docstring already said it reused the
other two's flow. One of those is now ESG and one is 10-K, so the copies sat on
opposite sides of the split with no owner. This is the fifth shared module.

Usage::

    from common import drive_auth

    service = drive_auth.get_service(drive_auth.READONLY_SCOPES)
    service = drive_auth.get_service(drive_auth.READWRITE_SCOPES)

Behaviour is deliberately unchanged from the three copies: same default
``token.json``, same ``GOOGLE_DRIVE_CLIENT_SECRET`` fallback to
``client_secret.json``, same refresh-then-reconsent sequence.

.. warning::

   **All callers share one token file while asking for different scopes.**
   That was true of the three copies and is preserved here rather than quietly
   changed. ``token.json`` holds whichever scope set authenticated last, so
   running the uploader after a read-only script can leave it holding
   ``drive.readonly`` — and the upload then fails at API-call time with a 403,
   not at login. ``get_service`` reports the mismatch through
   ``warn_on_scope_mismatch`` instead of silently proceeding.

   The real fix is one token file per scope set (pass ``token_path``), which
   costs a one-time re-consent. That is a live decision, not something this
   refactor should make on its own.
"""

from __future__ import annotations

import os
import warnings

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# The two scope sets the pipelines actually use.
READONLY_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
READWRITE_SCOPES = ["https://www.googleapis.com/auth/drive"]

DEFAULT_TOKEN_PATH = "token.json"
DEFAULT_CLIENT_SECRET_PATH = "client_secret.json"

_AUTH_PROMPT = (
    "\nOpen this URL in your browser if it does not open automatically:\n{url}\n"
)

# Keyed by (scopes, token_path): re-authenticating per call would re-read the
# token file for every Drive operation. drive_uploader.py cached this already.
_SERVICE_CACHE: dict[tuple, object] = {}


def client_secret_path() -> str:
    """Where the OAuth client secret lives, honouring the .env override."""
    return os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", DEFAULT_CLIENT_SECRET_PATH)


def load_credentials(
    scopes: list[str],
    token_path: str = DEFAULT_TOKEN_PATH,
    secret_path: str | None = None,
    warn_on_scope_mismatch: bool = True,
) -> Credentials:
    """Return usable credentials, refreshing or re-consenting as needed.

    Order of attempts, matching what the three copies did:

    1. Load ``token_path`` if it exists.
    2. If expired with a refresh token, refresh — and persist the result, so a
       silent refresh is not repeated on every run.
    3. Otherwise run the interactive desktop flow and write a new token.

    A dead refresh token (revoked, expired, scopes changed) falls through to
    the interactive flow rather than crashing.
    """
    secret_path = secret_path or client_secret_path()
    creds = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
        else:
            _write_token(creds, token_path)

    if not creds or not creds.valid:
        if not os.path.exists(secret_path):
            raise FileNotFoundError(
                f"OAuth client secret not found: {secret_path}. "
                f"Download it from Google Cloud Console > APIs & Services > "
                f"Credentials > OAuth 2.0 Client IDs, and place it at this path "
                f"(or set GOOGLE_DRIVE_CLIENT_SECRET in .env to point at it)."
            )

        flow = InstalledAppFlow.from_client_secrets_file(secret_path, scopes)
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            authorization_prompt_message=_AUTH_PROMPT,
        )
        _write_token(creds, token_path)

    if warn_on_scope_mismatch:
        _warn_if_under_scoped(creds, scopes, token_path)

    return creds


def get_service(
    scopes: list[str],
    token_path: str = DEFAULT_TOKEN_PATH,
    secret_path: str | None = None,
    cache: bool = True,
):
    """Return an authenticated Drive v3 service client."""
    key = (tuple(scopes), token_path)
    if cache and key in _SERVICE_CACHE:
        return _SERVICE_CACHE[key]

    creds = load_credentials(scopes, token_path=token_path, secret_path=secret_path)
    service = build("drive", "v3", credentials=creds)

    if cache:
        _SERVICE_CACHE[key] = service
    return service


def _write_token(creds: Credentials, token_path: str) -> None:
    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())


def _warn_if_under_scoped(
    creds: Credentials, requested: list[str], token_path: str
) -> None:
    """Surface the shared-token scope hazard at login instead of at 403.

    ``has_scopes`` compares scope strings literally, so a token carrying only
    ``drive.readonly`` does not satisfy a request for ``drive`` even though the
    reverse reads as "more access". Warn rather than raise: an existing working
    setup should not start failing because of a refactor.
    """
    granted = getattr(creds, "scopes", None)
    if not granted:
        return
    if set(requested).issubset(set(granted)):
        return
    warnings.warn(
        f"{token_path} was authenticated for {sorted(granted)} but this caller "
        f"asked for {sorted(requested)}. Drive calls needing the missing scope "
        f"will fail with a 403 at request time. Re-authenticate, or give this "
        f"caller its own token_path.",
        RuntimeWarning,
        stacklevel=3,
    )

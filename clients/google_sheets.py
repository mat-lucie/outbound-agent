"""Google Sheets client for writing prospect data."""

import contextlib
import json
import os
import shutil

import gspread

CREDENTIALS_DIR = os.path.join(os.path.dirname(__file__), "..", "credentials")
OAUTH_CREDENTIALS = os.path.join(CREDENTIALS_DIR, "google-oauth.json")
AUTHORIZED_USER = os.path.join(CREDENTIALS_DIR, "google-authorized-user.json")
AUTHORIZED_USER_BACKUP = AUTHORIZED_USER + ".bak"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _check_oauth_credentials() -> None:
    """Raise a clear RuntimeError if google-oauth.json is missing or 0-byte.

    gspread.oauth() crashes opaquely (KeyError / JSONDecodeError) when the
    credentials file is empty. Catching this early gives the operator an
    actionable error message instead of a cryptic stack trace deep inside
    google-auth. (T1.5c from operator-safety audit.)
    """
    if not os.path.exists(OAUTH_CREDENTIALS):
        raise RuntimeError(
            f"Missing OAuth credentials file: {OAUTH_CREDENTIALS}\n"
            "Fix: create a Google OAuth2 client secret JSON and save it to "
            "credentials/google-oauth.json. "
            "See docs at: https://docs.gspread.org/en/latest/oauth2.html"
        )
    if os.path.getsize(OAUTH_CREDENTIALS) == 0:
        raise RuntimeError(
            f"OAuth credentials file is 0 bytes: {OAUTH_CREDENTIALS}\n"
            "Fix: the file exists but is empty — re-download the OAuth2 client "
            "secret JSON from Google Cloud Console and save it to "
            "credentials/google-oauth.json."
        )


def get_client() -> gspread.Client:
    """Get an authenticated gspread client using OAuth2 credentials.

    gspread (via google-auth) writes the authorized-user token file
    non-atomically when refreshing access tokens. A network drop mid-write
    can truncate it to 0 bytes, after which gspread.oauth() crashes with
    JSONDecodeError on next start. We heal that here by restoring a known-
    good backup, or removing the broken file so a fresh interactive flow
    can run. After successful auth we refresh the backup atomically.
    """
    _heal_corrupt_token()
    # The client-secret preflight only applies when an INTERACTIVE flow
    # would run: gspread.oauth() never reads google-oauth.json while a
    # usable authorized-user token exists. Enforcing it unconditionally
    # would block sends when the client-secret file is 0-byte but the
    # authorized-user token is valid. (7bd45b3 fix.)
    if not _is_valid_token(AUTHORIZED_USER):
        _check_oauth_credentials()
    client = gspread.oauth(
        credentials_filename=OAUTH_CREDENTIALS,
        authorized_user_filename=AUTHORIZED_USER,
        scopes=SCOPES,
    )
    _backup_token()
    return client


def _is_valid_token(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return bool(data.get("refresh_token"))


def _heal_corrupt_token() -> None:
    if not os.path.exists(AUTHORIZED_USER) or _is_valid_token(AUTHORIZED_USER):
        return
    if _is_valid_token(AUTHORIZED_USER_BACKUP):
        shutil.copy2(AUTHORIZED_USER_BACKUP, AUTHORIZED_USER)
        print(f"  Restored corrupt OAuth token from {AUTHORIZED_USER_BACKUP}")
        return
    os.remove(AUTHORIZED_USER)
    print("  Removed corrupt OAuth token; gspread will trigger re-auth flow.")


def _backup_token() -> None:
    if not _is_valid_token(AUTHORIZED_USER):
        return
    tmp = f"{AUTHORIZED_USER_BACKUP}.tmp.{os.getpid()}"
    try:
        shutil.copy2(AUTHORIZED_USER, tmp)
        os.replace(tmp, AUTHORIZED_USER_BACKUP)
    except OSError:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)


def write_prospects_to_sheet(
    rows: list[dict],
    spreadsheet_id: str | None = None,
    worksheet_name: str = "Sheet1",
    columns: list[str] | None = None,
) -> str:
    """Write prospect rows to a Google Sheet, replacing all existing data.

    Columns default to ``["linkedInUrl", "message"]`` (the Network Booster /
    Message Sender input shape). Pass ``columns`` to override — e.g. Phase-0
    Profile Scraper expects a single ``profileUrl`` column.

    Row keys must match the requested column names exactly (case-sensitive).
    Returns the spreadsheet URL for use as a PB phantom input.
    """
    if columns is None:
        columns = ["linkedInUrl", "message"]
    sid = spreadsheet_id or os.environ["GSHEET_AUTOCONNECT_ID"]
    gc = get_client()
    sh = gc.open_by_key(sid)

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=len(rows) + 1, cols=len(columns))

    ws.clear()
    data = [columns] + [[row.get(col, "") for col in columns] for row in rows]
    ws.update(data, "A1")

    return f"https://docs.google.com/spreadsheets/d/{sid}"

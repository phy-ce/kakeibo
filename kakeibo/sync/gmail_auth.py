"""Gmail OAuth — creates a client from GOOGLE_CLIENT_ID/SECRET (or legacy credentials.json).

Secrets are stored in the user's home outside the project (~/.kakeibo).
- OAuth client: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in `~/.kakeibo/.env`
  (entered and saved from the console on first run). For backward compatibility, a credentials.json file is also supported.
- token: automatically saved to `~/.kakeibo/token.json` after the first consent -> reused afterward.
"""
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ..config import user_config_dir, user_path, resolve_secret_file, load_env
from ..i18n import t

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _client_config():
    """Builds a config dict for InstalledAppFlow from GOOGLE_CLIENT_ID/SECRET in .env (None if absent)."""
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csecret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not (cid and csecret):
        return None
    return {
        "installed": {
            "client_id": cid,
            "client_secret": csecret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }


def _build_flow():
    """Creates the OAuth flow. Prefers env values, falls back to legacy credentials.json, errors if neither exists."""
    cfg = _client_config()
    if cfg:
        return InstalledAppFlow.from_client_config(cfg, SCOPES)

    creds_file = resolve_secret_file("credentials.json")  # backward compatibility
    if os.path.exists(creds_file):
        return InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)

    raise RuntimeError(t('msg.gmail_not_configured', env=user_path('.env')))


def get_gmail_service():
    # Reflect the latest .env (also covers editing the file only, without the console) + ensure the folder exists
    load_env()
    os.makedirs(user_config_dir(), exist_ok=True)

    token_file = resolve_secret_file("token.json")
    token_out  = user_path("token.json")

    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = _build_flow()
            creds = flow.run_local_server(port=0)
        with open(token_out, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

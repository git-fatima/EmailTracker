"""Run locally once to obtain Gmail offline credentials."""
import os
import logging
from pathlib import Path
from workflow import SCOPES
LOG = logging.getLogger(__name__)

def save_token(path, credentials):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(credentials.to_json())
    os.replace(temp, path)


def authorize(state, client_file):
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_file), SCOPES, autogenerate_code_verifier=True
    )
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, access_type="offline", prompt="consent"
    )
    if not credentials.refresh_token:
        raise RuntimeError("Google did not return a refresh token; authorize again")
    save_token(state / "token.json", credentials)
    LOG.info("Authorization saved. Keep token.json private.")



if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    state = base / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    authorize(state, base / "credentials.json")
    print("Saved state/token.json. Copy client_id, client_secret and refresh_token into Redis config.")

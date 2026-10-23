"""One-time local helper to obtain a Gmail API refresh token.

This script is NOT part of the running server. Run it once, on your own
machine, to authorize the Gmail sending account. It prints only the
refresh token, which you paste into backend/.env as
GMAIL_API_REFRESH_TOKEN. Never commit the token.

Usage:

    python authorize_gmail.py \
        --client-id "YOUR_DESKTOP_OAUTH_CLIENT_ID" \
        --client-secret "YOUR_DESKTOP_OAUTH_CLIENT_SECRET"

Setup steps in Google Cloud Console (use a NEW project or the same project
but a separate OAuth client from the web sign-in client):
  1. Enable the Gmail API for the project.
  2. Configure the OAuth consent screen (External or Internal). Add the
     sender account as a test user if the app is in testing.
  3. Create an OAuth client ID of type "Desktop app".
  4. Run this script and complete the browser consent as the sender account.
  5. Put the printed refresh token into backend/.env.

The scope requested is exactly:
    https://www.googleapis.com/auth/gmail.send
"""

import argparse
import http.server
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.send"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib name)
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)

        self.server.auth_code = params.get("code", [None])[0]
        self.server.auth_error = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h3>Authorization complete.</h3>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *args):
        # Keep the console quiet; we print our own messages.
        return


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _describe_token_error(response) -> str:
    """Build a safe diagnostic from a token-endpoint error response.

    Surfaces only Google's HTTP status and the OAuth ``error`` /
    ``error_description`` fields. Never includes the client secret,
    authorization code, access/refresh tokens, or the request body.
    """
    parts = [f"HTTP {response.status_code}"]

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        description = payload.get("error_description")

        if isinstance(error, str) and error:
            parts.append(f"error={error}")

        if isinstance(description, str) and description:
            parts.append(f"error_description={description}")

    return "; ".join(parts)


def authorize(client_id: str, client_secret: str) -> str:
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"

    server = http.server.HTTPServer(
        ("127.0.0.1", port), _CallbackHandler
    )
    server.auth_code = None
    server.auth_error = None

    state = secrets.token_urlsafe(24)

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }

    url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    print("Open this URL and sign in as the Gmail sender account:")
    print(url)
    print()
    print("Waiting for the browser redirect ...")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    thread = threading.Thread(target=server.handle_request)
    thread.start()
    thread.join(timeout=300)

    if server.auth_error:
        raise SystemExit(f"Authorization was denied: {server.auth_error}")

    if not server.auth_code:
        raise SystemExit("No authorization code was received (timed out).")

    # The token request MUST reuse the exact redirect_uri string sent in the
    # authorization request, and the same Desktop OAuth client credentials.
    # Google rejects a mismatch with 400 "redirect_uri_mismatch" (or 401 for
    # client credential problems).
    response = httpx.post(
        TOKEN_URL,
        data={
            "code": server.auth_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )

    if response.status_code != 200:
        detail = _describe_token_error(response)
        raise SystemExit(
            f"Token exchange failed ({detail}). Check that this is a "
            "Desktop OAuth client, that the client ID/secret match it, and "
            "that the redirect URI is unchanged."
        )

    tokens = response.json()
    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Revoke prior access "
            "for this app at https://myaccount.google.com/permissions and "
            "run the script again."
        )

    return refresh_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    refresh_token = authorize(args.client_id, args.client_secret)

    print()
    print("Success. Add this line to backend/.env (keep it secret):")
    print(f"GMAIL_API_REFRESH_TOKEN={refresh_token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

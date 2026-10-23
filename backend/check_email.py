"""Layered email delivery diagnostic.

Run from the backend directory with a recipient address:

    python check_email.py you@example.com

Each layer is reported separately:

  1. DNS resolution
  2. TCP connect
  3. Authentication
  4. Sender acceptance
  5. Provider acceptance

Inbox delivery is only confirmed when you actually receive the test email.
No layer below can confirm that. Secrets and the OTP are never printed.
"""

import asyncio
import base64
import os
import smtplib
import socket
import ssl
import sys

from email.message import EmailMessage

import httpx

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(BACKEND_DIR, ".env"))

SMTP_TIMEOUT_SECONDS = 10
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_API_URL = "https://api.resend.com/emails"
SUBJECT = "Prices Indicator email diagnostic"

INBOX_NOTE = (
    "Inbox delivery is confirmed ONLY when the email actually arrives in "
    "the recipient's inbox. This check cannot confirm that."
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _test_body() -> str:
    return (
        "This is a diagnostic email from Prices Indicator.\n"
        "If you received it, the configured provider can deliver mail.\n"
    )


def layer(name: str, status: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")


def _describe_oauth_error(response) -> str:
    """Build a safe diagnostic from a Google OAuth error response.

    Surfaces only the HTTP status plus the ``error`` and
    ``error_description`` fields (e.g. invalid_client, invalid_grant).
    Never includes the client secret, refresh/access tokens,
    authorization headers, or the request body.
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
            safe = "".join(
                char
                for char in description
                if char.isprintable() or char == " "
            )[:200]
            if safe:
                parts.append(f"error_description={safe}")

    return "; ".join(parts)


def check_smtp(to_email: str) -> bool:
    host = _env("SMTP_HOST")
    username = _env("SMTP_USERNAME")
    password = _env("SMTP_PASSWORD")
    from_email = _env("SMTP_FROM_EMAIL") or username
    from_name = _env("SMTP_FROM_NAME", "Prices Indicator")
    use_ssl = _truthy(_env("SMTP_USE_SSL", "false"))
    use_tls = _truthy(_env("SMTP_USE_TLS", "true"))

    if not host or not username or not password or not from_email:
        layer("configuration", "FAIL", "SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/SMTP_FROM_EMAIL required")
        return False

    if not use_ssl and not use_tls:
        layer("configuration", "FAIL", "SMTP_USE_TLS and SMTP_USE_SSL are both off")
        return False

    try:
        port = int(_env("SMTP_PORT", "465" if use_ssl else "587"))
    except ValueError:
        layer("configuration", "FAIL", "SMTP_PORT is not a number")
        return False

    print(f"Provider: smtp ({host}:{port})")
    print(f"Sender address: {from_email}")

    # Layer 1: DNS resolution.
    try:
        addresses = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        layer("DNS resolution", "PASS", f"{host} resolved")
    except socket.gaierror as exc:
        layer("DNS resolution", "FAIL", f"{type(exc).__name__}")
        return False

    # Layer 2: TCP connect.
    try:
        with socket.create_connection(
            addresses[0][4], timeout=SMTP_TIMEOUT_SECONDS
        ):
            layer("TCP connect", "PASS", f"port {port} reachable at the network layer")
    except (OSError, socket.timeout) as exc:
        layer("TCP connect", "FAIL", f"{type(exc).__name__}")
        layer(
            "network note",
            "INFO",
            "an ISP, firewall, or port block can stop SMTP ports even when DNS works",
        )
        return False

    # Layer 3: Authentication plus layers 4/5 (sender and provider acceptance).
    message = EmailMessage()
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = to_email
    message["Subject"] = SUBJECT
    message.set_content(_test_body())

    context = ssl.create_default_context()

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(
                host,
                port,
                timeout=SMTP_TIMEOUT_SECONDS,
                context=context,
            )
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
    except (TimeoutError, socket.timeout):
        layer("TCP connect", "FAIL", "timeout while opening the SMTP session")
        return False
    except OSError as exc:
        layer("TCP connect", "FAIL", f"{type(exc).__name__}")
        return False

    try:
        with server:
            if not use_ssl:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()

            try:
                server.login(username, password)
                layer("authentication", "PASS", "login accepted")
            except smtplib.SMTPAuthenticationError:
                layer("authentication", "FAIL", "provider rejected the credentials")
                return False
            except Exception as exc:
                layer("authentication", "FAIL", f"{type(exc).__name__}")
                return False

            try:
                server.send_message(message)
                layer("sender acceptance", "PASS", "server accepted the sender")
                layer("provider acceptance", "PASS", "server accepted the message for delivery")
            except smtplib.SMTPSenderRefused:
                layer("sender acceptance", "FAIL", "sender address was refused")
                return False
            except smtplib.SMTPRecipientsRefused:
                layer("provider acceptance", "FAIL", "recipient was refused")
                return False
            except smtplib.SMTPException as exc:
                layer("provider acceptance", "FAIL", f"{type(exc).__name__}")
                return False
    except (TimeoutError, socket.timeout):
        layer("network", "FAIL", "timeout during the SMTP session")
        return False
    except OSError as exc:
        layer("network", "FAIL", f"{type(exc).__name__}")
        return False

    print(INBOX_NOTE)
    return True


def _http_layer_report(url: str, headers: dict, payload: dict) -> bool:
    # Layer 1: DNS resolution.
    host = httpx.URL(url).host
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        layer("DNS resolution", "PASS", f"{host} resolved")
    except socket.gaierror as exc:
        layer("DNS resolution", "FAIL", f"{type(exc).__name__}")
        return False

    # Layer 2: TCP connect is exercised by the HTTPS request below.
    try:
        response = httpx.post(
            url,
            headers=headers,
            json=payload,
            timeout=10,
            follow_redirects=False,
            verify=True,
        )
        layer("TCP connect", "PASS", f"HTTPS reachable to {host}:443")
    except httpx.TimeoutException:
        layer("TCP connect", "FAIL", "request timed out")
        return False
    except httpx.ConnectError as exc:
        layer("TCP connect", "FAIL", f"{type(exc).__name__}")
        return False
    except httpx.HTTPError as exc:
        layer("TCP connect", "FAIL", f"{type(exc).__name__}")
        return False

    if response.status_code in (200, 201, 202):
        layer("authentication", "PASS", "API key accepted")
        layer("sender acceptance", "PASS", "sender accepted")
        layer("provider acceptance", "PASS", "message accepted for delivery")
        print(INBOX_NOTE)
        return True

    if response.status_code == 401:
        layer("authentication", "FAIL", "API key rejected")
        return False

    if response.status_code in (400, 403, 422):
        layer("authentication", "PASS", "API key accepted")
        layer(
            "sender acceptance",
            "FAIL",
            f"provider rejected sender or recipient (HTTP {response.status_code})",
        )
        return False

    layer("provider acceptance", "FAIL", f"HTTP {response.status_code}")
    return False


def check_brevo(to_email: str) -> bool:
    api_key = _env("BREVO_API_KEY")
    sender_email = _env("BREVO_SENDER_EMAIL")
    sender_name = _env("BREVO_SENDER_NAME", "Prices Indicator")

    if not api_key or not sender_email:
        layer("configuration", "FAIL", "BREVO_API_KEY and BREVO_SENDER_EMAIL required")
        return False

    print("Provider: brevo (HTTPS API)")
    print(f"Sender address: {sender_email}")

    return _http_layer_report(
        BREVO_API_URL,
        {"api-key": api_key, "Content-Type": "application/json"},
        {
            "sender": {"email": sender_email, "name": sender_name},
            "to": [{"email": to_email}],
            "subject": SUBJECT,
            "textContent": _test_body(),
        },
    )


def check_resend(to_email: str) -> bool:
    api_key = _env("RESEND_API_KEY")
    from_email = _env("RESEND_FROM_EMAIL")
    from_name = _env("RESEND_FROM_NAME", "Prices Indicator")

    if not api_key or not from_email:
        layer("configuration", "FAIL", "RESEND_API_KEY and RESEND_FROM_EMAIL required")
        return False

    print("Provider: resend (HTTPS API)")
    print(f"Sender address: {from_email}")

    if from_email.rsplit("@", 1)[-1].lower() == "resend.dev":
        layer(
            "configuration",
            "INFO",
            "the resend.dev testing sender only reaches the Resend account owner",
        )

    return _http_layer_report(
        RESEND_API_URL,
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {
            "from": f"{from_name} <{from_email}>",
            "to": [to_email],
            "subject": SUBJECT,
            "text": _test_body(),
        },
    )


def check_gmail_api(to_email: str) -> bool:
    client_id = _env("GMAIL_API_CLIENT_ID")
    client_secret = _env("GMAIL_API_CLIENT_SECRET")
    refresh_token = _env("GMAIL_API_REFRESH_TOKEN")
    sender_email = _env("GMAIL_API_SENDER_EMAIL")
    sender_name = _env("GMAIL_API_SENDER_NAME", "Prices Indicator")

    if (
        not client_id
        or not client_secret
        or not refresh_token
        or not sender_email
    ):
        layer(
            "configuration",
            "FAIL",
            "GMAIL_API_CLIENT_ID/GMAIL_API_CLIENT_SECRET/"
            "GMAIL_API_REFRESH_TOKEN/GMAIL_API_SENDER_EMAIL required",
        )
        return False

    print("Provider: gmail_api (HTTPS API)")
    print(f"Sender address: {sender_email}")

    # Layer 1: DNS resolution.
    for host in ("oauth2.googleapis.com", "gmail.googleapis.com"):
        try:
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            layer("DNS resolution", "PASS", f"{host} resolved")
        except socket.gaierror as exc:
            layer("DNS resolution", "FAIL", f"{host}: {type(exc).__name__}")
            return False

    # Layer 3a: refresh-token exchange (authentication).
    try:
        token_response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
    except httpx.TimeoutException:
        layer("authentication", "FAIL", "token refresh timed out")
        return False
    except httpx.HTTPError as exc:
        layer("authentication", "FAIL", f"{type(exc).__name__}")
        return False

    if token_response.status_code != 200:
        layer(
            "authentication",
            "FAIL",
            "refresh token or client credentials rejected "
            f"({_describe_oauth_error(token_response)})",
        )
        return False

    access_token = token_response.json().get("access_token")

    if not access_token:
        layer("authentication", "FAIL", "no access token returned")
        return False

    layer("authentication", "PASS", "refresh token exchanged for access token")

    # Layers 4/5: send via Gmail API.
    message = EmailMessage()
    message["From"] = f"{sender_name} <{sender_email}>"
    message["To"] = to_email
    message["Subject"] = SUBJECT
    message.set_content(_test_body())

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    try:
        send_response = httpx.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw},
            timeout=10,
        )
    except httpx.TimeoutException:
        layer("provider acceptance", "FAIL", "send request timed out")
        return False
    except httpx.HTTPError as exc:
        layer("provider acceptance", "FAIL", f"{type(exc).__name__}")
        return False

    if 200 <= send_response.status_code < 300:
        layer("sender acceptance", "PASS", "sender accepted")
        layer("provider acceptance", "PASS", "message accepted for delivery")
        print(INBOX_NOTE)
        return True

    if send_response.status_code in (401, 403):
        layer("authentication", "FAIL", "Gmail API rejected the access token")
        return False

    layer(
        "sender acceptance",
        "FAIL",
        f"Gmail API rejected the request (HTTP {send_response.status_code})",
    )
    return False


def main() -> int:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        print("Usage: python check_email.py you@example.com")
        return 2

    to_email = sys.argv[1].strip()
    provider = _env("EMAIL_PROVIDER", "smtp").lower()

    print(f"Diagnostic recipient: {to_email}")
    print("")

    if provider == "smtp":
        ok = check_smtp(to_email)
    elif provider == "brevo":
        ok = check_brevo(to_email)
    elif provider == "resend":
        ok = check_resend(to_email)
    elif provider == "gmail_api":
        ok = check_gmail_api(to_email)
    else:
        layer("configuration", "FAIL", f"unknown EMAIL_PROVIDER '{provider}'")
        return 1

    print("")
    if ok:
        print("All requestable layers passed. Confirm delivery by checking the inbox.")
        return 0

    print("One or more layers failed. Delivery was not confirmed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

import asyncio
import base64
import html
import os
import smtplib
import socket
import ssl

from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

SMTP_TIMEOUT_SECONDS = 10
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_API_URL = "https://api.resend.com/emails"
RESEND_USER_AGENT = "PricesIndicator/1.0"
GMAIL_SEND_URL = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
)
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

SUBJECT = "Verify your email address \u2014 Prices Indicator"
PASSWORD_RESET_SUBJECT = "Reset your password \u2014 Prices Indicator"


class EmailSendError(RuntimeError):
    def __init__(self, category: str):
        self.category = category
        super().__init__(f"Email delivery failed ({category}).")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _otp_text(name: str, otp: str) -> str:
    greeting_name = (name or "").strip() or "there"

    return (
        f"Hello {greeting_name},\n\n"
        "Welcome to Prices Indicator!\n\n"
        "We're excited to have you join us. To complete your registration "
        "and verify your email address, please enter the verification "
        "code below:\n\n"
        f"{otp}\n\n"
        "This code expires in 10 min. For your security, please do not "
        "share this code with anyone.\n\n"
        "If you did not request this registration, you can safely ignore "
        "this email.\n\n"
        "Best regards,\n"
        "The Prices Indicator Team\n"
    )


def _otp_html(name: str, otp: str) -> str:
    greeting_name = (name or "").strip() or "there"
    safe_name = html.escape(greeting_name)
    safe_otp = html.escape(otp)

    return (
        "<!doctype html><html><body "
        "style=\"font-family:Arial,Helvetica,sans-serif;color:#111;\">"
        f"<p>Hello {safe_name},</p>"
        "<p>Welcome to Prices Indicator!</p>"
        "<p>We're excited to have you join us. To complete your "
        "registration and verify your email address, please enter the "
        "verification code below:</p>"
        f"<p style=\"font-size:28px;font-weight:bold;"
        f"letter-spacing:4px;\">{safe_otp}</p>"
        "<p>This code expires in 10 min. For your security, please do "
        "not share this code with anyone.</p>"
        "<p>If you did not request this registration, you can safely ignore "
        "this email.</p>"
        "<p>Best regards,<br>The Prices Indicator Team</p>"
        "</body></html>"
    )


def _password_reset_text(name: str, otp: str) -> str:
    greeting_name = (name or "").strip() or "there"

    return (
        f"Hello {greeting_name},\n\n"
        "We received a request to reset the password for your Prices "
        "Indicator account.\n\n"
        "Enter this password reset code to continue:\n\n"
        f"{otp}\n\n"
        "This code expires in 10 min. For your security, please do not "
        "share this code with anyone.\n\n"
        "If you did not request a password reset, you can safely ignore "
        "this email. Your password will not be changed.\n\n"
        "Best regards,\n"
        "The Prices Indicator Team\n"
    )


def _password_reset_html(name: str, otp: str) -> str:
    greeting_name = (name or "").strip() or "there"
    safe_name = html.escape(greeting_name)
    safe_otp = html.escape(otp)

    return (
        "<!doctype html><html><body "
        "style=\"font-family:Arial,Helvetica,sans-serif;color:#111;\">"
        f"<p>Hello {safe_name},</p>"
        "<p>We received a request to reset the password for your Prices "
        "Indicator account.</p>"
        "<p>Enter this password reset code to continue:</p>"
        f"<p style=\"font-size:28px;font-weight:bold;"
        f"letter-spacing:4px;\">{safe_otp}</p>"
        "<p>This code expires in 10 min. For your security, please do "
        "not share this code with anyone.</p>"
        "<p>If you did not request a password reset, you can safely ignore "
        "this email. Your password will not be changed.</p>"
        "<p>Best regards,<br>The Prices Indicator Team</p>"
        "</body></html>"
    )


def _email_content(
    purpose: str,
    name: str,
    otp: str,
) -> tuple[str, str, str]:
    if purpose == "password_reset":
        return (
            PASSWORD_RESET_SUBJECT,
            _password_reset_text(name, otp),
            _password_reset_html(name, otp),
        )

    return SUBJECT, _otp_text(name, otp), _otp_html(name, otp)


def _smtp_settings() -> dict:
    host = _env("SMTP_HOST")
    username = _env("SMTP_USERNAME")
    password = _env("SMTP_PASSWORD")
    from_email = _env("SMTP_FROM_EMAIL") or username
    from_name = _env("SMTP_FROM_NAME", "Prices Indicator")

    if not host or not username or not password or not from_email:
        raise EmailSendError("config")

    use_ssl = _truthy(_env("SMTP_USE_SSL", "false"))
    use_tls = _truthy(_env("SMTP_USE_TLS", "true"))

    if not use_ssl and not use_tls:
        raise EmailSendError("config")

    try:
        port = int(_env("SMTP_PORT", "465" if use_ssl else "587"))
    except ValueError as exc:
        raise EmailSendError("config") from exc

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from_email": from_email,
        "from_name": from_name,
        "use_ssl": use_ssl,
    }


def _smtp_send(
    to_email: str,
    otp: str,
    name: str = "",
    purpose: str = "signup",
) -> None:
    settings = _smtp_settings()
    subject, text_content, html_content = _email_content(
        purpose, name, otp
    )

    message = EmailMessage()
    message["From"] = (
        f"{settings['from_name']} <{settings['from_email']}>"
    )
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(text_content)
    message.add_alternative(html_content, subtype="html")

    context = ssl.create_default_context()

    try:
        if settings["use_ssl"]:
            with smtplib.SMTP_SSL(
                settings["host"],
                settings["port"],
                timeout=SMTP_TIMEOUT_SECONDS,
                context=context,
            ) as server:
                server.login(
                    settings["username"],
                    settings["password"],
                )
                server.send_message(message)
        else:
            with smtplib.SMTP(
                settings["host"],
                settings["port"],
                timeout=SMTP_TIMEOUT_SECONDS,
            ) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(
                    settings["username"],
                    settings["password"],
                )
                server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailSendError("auth") from exc
    except (
        smtplib.SMTPSenderRefused,
        smtplib.SMTPRecipientsRefused,
    ) as exc:
        raise EmailSendError("sender") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise EmailSendError("timeout") from exc
    except (
        smtplib.SMTPConnectError,
        smtplib.SMTPServerDisconnected,
        OSError,
    ) as exc:
        raise EmailSendError("network") from exc
    except smtplib.SMTPException as exc:
        raise EmailSendError("unknown") from exc


async def _send_smtp(
    to_email: str,
    otp: str,
    name: str = "",
    purpose: str = "signup",
) -> None:
    await asyncio.to_thread(
        _smtp_send, to_email, otp, name, purpose
    )


def _http_category(status_code: int, provider: str, body: str) -> str:
    text = body.lower()

    if status_code == 401:
        return "auth"

    if status_code == 403:
        if "api key" in text or "unauthorized" in text:
            return "auth"
        return "sender"

    if status_code in (400, 422):
        if any(
            marker in text
            for marker in ("api key", "unauthorized", "restricted")
        ):
            return "auth"
        return "sender"

    return "unknown"


async def _post_email(
    url: str,
    headers: dict,
    payload: dict,
    provider: str,
) -> None:
    try:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=False,
            verify=True,
        ) as client:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise EmailSendError("timeout") from exc
    except (httpx.ConnectError, httpx.NetworkError) as exc:
        raise EmailSendError("network") from exc
    except httpx.HTTPError as exc:
        raise EmailSendError("unknown") from exc

    if 200 <= response.status_code < 300:
        return

    body = response.text[:2000]
    raise EmailSendError(
        _http_category(response.status_code, provider, body)
    )


async def _send_brevo(
    to_email: str,
    otp: str,
    name: str = "",
    purpose: str = "signup",
) -> None:
    api_key = _env("BREVO_API_KEY")
    sender_email = _env("BREVO_SENDER_EMAIL")
    sender_name = _env("BREVO_SENDER_NAME", "Prices Indicator")
    subject, text_content, html_content = _email_content(
        purpose, name, otp
    )

    if not api_key or not sender_email:
        raise EmailSendError("config")

    await _post_email(
        BREVO_API_URL,
        {
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        {
            "sender": {
                "email": sender_email,
                "name": sender_name,
            },
            "to": [{"email": to_email}],
            "subject": subject,
            "textContent": text_content,
            "htmlContent": html_content,
        },
        "brevo",
    )


async def _send_resend(
    to_email: str,
    otp: str,
    name: str = "",
    purpose: str = "signup",
) -> None:
    api_key = _env("RESEND_API_KEY")
    from_email = _env("RESEND_FROM_EMAIL")
    from_name = _env("RESEND_FROM_NAME", "Prices Indicator")
    subject, text_content, html_content = _email_content(
        purpose, name, otp
    )

    if not api_key or not from_email:
        raise EmailSendError("config")

    await _post_email(
        RESEND_API_URL,
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": RESEND_USER_AGENT,
        },
        {
            "from": f"{from_name} <{from_email}>",
            "to": [to_email],
            "subject": subject,
            "text": text_content,
            "html": html_content,
        },
        "resend",
    )


def _gmail_settings() -> dict:
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
        raise EmailSendError("config")

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "sender_email": sender_email,
        "sender_name": sender_name,
    }


def _gmail_access_token(settings: dict) -> str:
    # google-auth performs the OAuth refresh-token exchange over HTTPS and
    # validates the token audience. No refresh token is ever logged.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    credentials = Credentials(
        token=None,
        refresh_token=settings["refresh_token"],
        token_uri=GMAIL_TOKEN_URL,
        client_id=settings["client_id"],
        client_secret=settings["client_secret"],
        scopes=[GMAIL_SEND_SCOPE],
    )

    try:
        credentials.refresh(Request())
    except Exception as exc:
        # Refresh failures are auth problems (bad/revoked/expired token).
        raise EmailSendError("auth") from exc

    if not credentials.token:
        raise EmailSendError("auth")

    return credentials.token


def _gmail_raw_message(
    to_email: str,
    otp: str,
    settings: dict,
    name: str = "",
    purpose: str = "signup",
) -> str:
    subject, text_content, html_content = _email_content(
        purpose, name, otp
    )

    message = MIMEMultipart("alternative")
    message["To"] = to_email
    message["From"] = (
        f"{settings['sender_name']} <{settings['sender_email']}>"
    )
    message["Subject"] = subject
    message.attach(MIMEText(text_content, "plain", "utf-8"))
    message.attach(MIMEText(html_content, "html", "utf-8"))

    return base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode("ascii")


async def _send_gmail_api(
    to_email: str,
    otp: str,
    name: str = "",
    purpose: str = "signup",
) -> None:
    settings = _gmail_settings()

    # Run the blocking token refresh off the event loop.
    access_token = await asyncio.to_thread(
        _gmail_access_token, settings
    )

    raw_message = _gmail_raw_message(
        to_email, otp, settings, name, purpose
    )

    try:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=False,
            verify=True,
        ) as client:
            response = await client.post(
                GMAIL_SEND_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"raw": raw_message},
            )
    except httpx.TimeoutException as exc:
        raise EmailSendError("timeout") from exc
    except (httpx.ConnectError, httpx.NetworkError) as exc:
        raise EmailSendError("network") from exc
    except httpx.HTTPError as exc:
        raise EmailSendError("unknown") from exc

    if 200 <= response.status_code < 300:
        return

    if response.status_code in (401, 403):
        raise EmailSendError("auth")

    if response.status_code in (400, 422):
        raise EmailSendError("sender")

    raise EmailSendError("unknown")


async def send_otp_email(
    to_email: str,
    otp: str,
    name: str = "",
    purpose: str = "signup",
) -> None:
    provider = _env("EMAIL_PROVIDER", "smtp").lower()

    if provider == "smtp":
        await _send_smtp(to_email, otp, name, purpose)
    elif provider == "brevo":
        await _send_brevo(to_email, otp, name, purpose)
    elif provider == "resend":
        await _send_resend(to_email, otp, name, purpose)
    elif provider == "gmail_api":
        await _send_gmail_api(to_email, otp, name, purpose)
    else:
        raise EmailSendError("config")


async def send_password_reset_email(
    to_email: str,
    otp: str,
    name: str = "",
) -> None:
    await send_otp_email(
        to_email,
        otp,
        name,
        purpose="password_reset",
    )

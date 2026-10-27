import asyncio
import base64
import smtplib
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from pathlib import Path
from unittest.mock import patch

import httpx
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

import email_service
import main
from database import DATABASE_URL, Base, get_db
from email_service import EmailSendError
from models import OTPChallenge, User

import authorize_gmail
import check_email

TEST_OTP = "654321"


def _wrong_otp(correct: str) -> str:
    return "000001" if correct == "000000" else "000000"


class FakeSmtpServer:
    def __init__(self, login_error=None, send_error=None):
        self.login_error = login_error
        self.send_error = send_error
        self.logged_in = False
        self.sent_messages = []
        self.started_tls = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def ehlo(self):
        return None

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, username, password):
        if self.login_error is not None:
            raise self.login_error
        self.logged_in = True

    def send_message(self, message):
        if self.send_error is not None:
            raise self.send_error
        self.sent_messages.append(message)


class FakeHttpClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(
            {"url": url, "headers": headers or {}, "json": json or {}}
        )

        if self.error is not None:
            raise self.error

        if self.response is not None:
            return self.response

        return httpx.Response(
            200,
            json={"id": "email-id"},
            request=httpx.Request("POST", url),
        )


def http_response(status_code: int, url: str, body=None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=body or {},
        request=httpx.Request("POST", url),
    )


def _smtp_environment(provider: str):
    return patch.dict(
        "os.environ",
        {
            "EMAIL_PROVIDER": provider,
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USE_TLS": "true",
            "SMTP_USE_SSL": "false",
            "SMTP_USERNAME": "sender@example.test",
            "SMTP_PASSWORD": "secret",
            "SMTP_FROM_EMAIL": "sender@example.test",
            "SMTP_FROM_NAME": "Prices Indicator",
            "BREVO_API_KEY": "brevo_test_key",
            "BREVO_SENDER_EMAIL": "sender@example.test",
            "BREVO_SENDER_NAME": "Prices Indicator",
            "RESEND_API_KEY": "re_test_key",
            "RESEND_FROM_EMAIL": "sender@example.test",
            "RESEND_FROM_NAME": "Prices Indicator",
            "GMAIL_API_CLIENT_ID": "gmail-client-id.apps.googleusercontent.com",
            "GMAIL_API_CLIENT_SECRET": "gmail-client-secret",
            "GMAIL_API_REFRESH_TOKEN": "1//gmail-refresh-token",
            "GMAIL_API_SENDER_EMAIL": "sender@example.test",
            "GMAIL_API_SENDER_NAME": "Prices Indicator",
        },
    )


def _gmail_refresh_patch():
    # google-auth refreshes tokens via its own transport. Patch Credentials
    # so tests never hit Google's token endpoint.
    return patch(
        "google.oauth2.credentials.Credentials.refresh",
        lambda self, request: setattr(
            self, "token", "ya29.test-access-token"
        ),
    )


class FakeGmailTransport:
    """Routes httpx calls: token exchange vs. Gmail send.

    Google-auth performs the refresh-token exchange through its own
    transport, so we patch that separately. This fake covers the send call.
    """

    def __init__(self, send_response=None, send_error=None):
        self.send_response = send_response
        self.send_error = send_error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(
            {"url": url, "headers": headers or {}, "json": json or {}}
        )

        if self.send_error is not None:
            raise self.send_error

        if self.send_response is not None:
            return self.send_response

        return httpx.Response(
            200,
            json={"id": "gmail-message-id"},
            request=httpx.Request("POST", url),
        )


class EmailServiceTests(unittest.IsolatedAsyncioTestCase):
    async def send_email(self):
        await email_service.send_otp_email(
            "recipient@example.test",
            TEST_OTP,
        )

    async def assert_fails(self, category):
        with self.assertRaises(EmailSendError) as caught:
            await self.send_email()
        self.assertEqual(caught.exception.category, category)

    async def test_smtp_success_uses_starttls_and_login(self):
        server = FakeSmtpServer()

        with _smtp_environment("smtp"), patch(
            "smtplib.SMTP", return_value=server
        ):
            await self.send_email()

        self.assertTrue(server.logged_in)
        self.assertTrue(server.started_tls)
        self.assertEqual(len(server.sent_messages), 1)
        message = server.sent_messages[0]
        bodies = "".join(
            part.get_content()
            for part in message.walk()
            if part.get_content_maintype() == "text"
        )
        self.assertIn(TEST_OTP, bodies)

    async def test_smtp_auth_failure_is_categorised(self):
        server = FakeSmtpServer(
            login_error=smtplib.SMTPAuthenticationError(535, b"bad")
        )

        with _smtp_environment("smtp"), patch(
            "smtplib.SMTP", return_value=server
        ):
            await self.assert_fails("auth")

    async def test_smtp_sender_rejection_is_categorised(self):
        server = FakeSmtpServer(
            send_error=smtplib.SMTPSenderRefused(
                550, b"bad sender", "sender@example.test"
            )
        )

        with _smtp_environment("smtp"), patch(
            "smtplib.SMTP", return_value=server
        ):
            await self.assert_fails("sender")

    async def test_smtp_timeout_is_categorised(self):
        with _smtp_environment("smtp"), patch(
            "smtplib.SMTP", side_effect=TimeoutError("timed out")
        ):
            await self.assert_fails("timeout")

    async def test_smtp_network_error_is_categorised(self):
        with _smtp_environment("smtp"), patch(
            "smtplib.SMTP", side_effect=OSError("unreachable")
        ):
            await self.assert_fails("network")

    async def test_smtp_missing_config_is_categorised(self):
        with patch.dict(
            "os.environ",
            {"EMAIL_PROVIDER": "smtp", "SMTP_HOST": ""},
        ):
            await self.assert_fails("config")

    async def test_smtp_ssl_uses_smtp_ssl(self):
        server = FakeSmtpServer()

        with patch.dict(
            "os.environ",
            {
                "EMAIL_PROVIDER": "smtp",
                "SMTP_HOST": "smtp.example.test",
                "SMTP_PORT": "465",
                "SMTP_USE_TLS": "false",
                "SMTP_USE_SSL": "true",
                "SMTP_USERNAME": "sender@example.test",
                "SMTP_PASSWORD": "secret",
                "SMTP_FROM_EMAIL": "sender@example.test",
            },
        ), patch("smtplib.SMTP_SSL", return_value=server) as ssl_mock:
            await self.send_email()

        self.assertEqual(ssl_mock.call_count, 1)
        self.assertTrue(server.logged_in)

    async def test_brevo_success_posts_documented_payload(self):
        client = FakeHttpClient()

        with _smtp_environment("brevo"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=client,
        ):
            await self.send_email()

        call = client.calls[0]
        self.assertEqual(call["url"], email_service.BREVO_API_URL)
        self.assertEqual(call["headers"]["api-key"], "brevo_test_key")
        self.assertEqual(
            call["json"]["to"], [{"email": "recipient@example.test"}]
        )
        self.assertIn(TEST_OTP, call["json"]["textContent"])

    async def test_brevo_auth_failure_is_categorised(self):
        client = FakeHttpClient(
            response=http_response(
                401,
                email_service.BREVO_API_URL,
                {"message": "Key not found"},
            )
        )

        with _smtp_environment("brevo"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=client,
        ):
            await self.assert_fails("auth")

    async def test_brevo_sender_failure_is_categorised(self):
        client = FakeHttpClient(
            response=http_response(
                400,
                email_service.BREVO_API_URL,
                {"message": "sender not valid"},
            )
        )

        with _smtp_environment("brevo"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=client,
        ):
            await self.assert_fails("sender")

    async def test_brevo_timeout_is_categorised(self):
        client = FakeHttpClient(error=httpx.ReadTimeout("timed out"))

        with _smtp_environment("brevo"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=client,
        ):
            await self.assert_fails("timeout")

    async def test_resend_success_posts_documented_payload(self):
        client = FakeHttpClient()

        with _smtp_environment("resend"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=client,
        ):
            await self.send_email()

        call = client.calls[0]
        self.assertEqual(call["url"], email_service.RESEND_API_URL)
        self.assertEqual(
            call["headers"]["Authorization"], "Bearer re_test_key"
        )
        self.assertEqual(
            call["json"]["from"],
            "Prices Indicator <sender@example.test>",
        )

    async def test_resend_auth_failure_is_categorised(self):
        client = FakeHttpClient(
            response=http_response(
                401,
                email_service.RESEND_API_URL,
                {"message": "API key is invalid"},
            )
        )

        with _smtp_environment("resend"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=client,
        ):
            await self.assert_fails("auth")

    async def test_unknown_provider_is_config_error(self):
        with patch.dict("os.environ", {"EMAIL_PROVIDER": "carrier-pigeon"}):
            await self.assert_fails("config")

    async def test_gmail_api_missing_config_is_config_error(self):
        with patch.dict(
            "os.environ",
            {
                "EMAIL_PROVIDER": "gmail_api",
                "GMAIL_API_CLIENT_ID": "",
            },
        ):
            await self.assert_fails("config")

    async def test_gmail_api_success_sends_base64url_mime(self):
        transport = FakeGmailTransport()

        with _smtp_environment("gmail_api"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=transport,
        ), _gmail_refresh_patch():
            await self.send_email()

        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["url"], email_service.GMAIL_SEND_URL)
        self.assertEqual(
            call["headers"]["Authorization"],
            "Bearer ya29.test-access-token",
        )

        raw = call["json"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        self.assertIn(b"Content-Type: multipart/alternative", decoded)
        self.assertIn(b"Prices Indicator", decoded)

        message = message_from_bytes(decoded)
        bodies = b"".join(
            base64.b64decode(part.get_payload())
            for part in message.walk()
            if part.get_content_maintype() == "text"
        )
        self.assertIn(TEST_OTP.encode("ascii"), bodies)

    async def test_gmail_api_token_refresh_failure_is_auth(self):
        def failing_refresh(self, request):
            raise ValueError("invalid_grant")

        with _smtp_environment("gmail_api"), patch(
            "google.oauth2.credentials.Credentials.refresh",
            failing_refresh,
        ):
            await self.assert_fails("auth")

    async def test_gmail_api_send_auth_failure_is_categorised(self):
        transport = FakeGmailTransport(
            send_response=httpx.Response(
                401,
                json={"error": {"message": "Invalid Credentials"}},
                request=httpx.Request(
                    "POST", email_service.GMAIL_SEND_URL
                ),
            )
        )

        with _smtp_environment("gmail_api"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=transport,
        ), _gmail_refresh_patch():
            await self.assert_fails("auth")

    async def test_gmail_api_send_sender_rejection_is_categorised(self):
        transport = FakeGmailTransport(
            send_response=httpx.Response(
                400,
                json={"error": {"message": "Invalid To header"}},
                request=httpx.Request(
                    "POST", email_service.GMAIL_SEND_URL
                ),
            )
        )

        with _smtp_environment("gmail_api"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=transport,
        ), _gmail_refresh_patch():
            await self.assert_fails("sender")

    async def test_gmail_api_timeout_is_categorised(self):
        transport = FakeGmailTransport(
            send_error=httpx.ReadTimeout("timed out")
        )

        with _smtp_environment("gmail_api"), patch.object(
            email_service.httpx,
            "AsyncClient",
            return_value=transport,
        ), _gmail_refresh_patch():
            await self.assert_fails("timeout")

    async def test_error_never_leaks_secrets_or_otp(self):
        server = FakeSmtpServer(
            login_error=smtplib.SMTPAuthenticationError(535, b"bad")
        )

        with _smtp_environment("smtp"), patch(
            "smtplib.SMTP", return_value=server
        ):
            with self.assertRaises(EmailSendError) as caught:
                await self.send_email()

        message = str(caught.exception)
        self.assertNotIn("secret", message)
        self.assertNotIn(TEST_OTP, message)


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        )
        self.original_send = main.send_otp_email

        async def fake_send(email: str, otp: str, name: str = "") -> None:
            return None

        main.send_otp_email = fake_send
        main.rate_limiter.reset()

    async def asyncTearDown(self):
        await self.client.aclose()
        main.send_otp_email = self.original_send
        main.rate_limiter.reset()

    async def test_signup_is_rate_limited_per_email(self):
        payload = {
            "name": "Test User",
            "email": "rate-limited@example.com",
            "password": "Password123!",
        }
        limit = main.RATE_LIMIT_SIGNUP[0]

        statuses = []
        for _ in range(limit + 1):
            response = await self.client.post("/auth/signup", json=payload)
            statuses.append(response.status_code)

        self.assertEqual(statuses[-1], 429)
        self.assertTrue(all(code != 429 for code in statuses[:limit]))


class AuthFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = f"test_auth_{uuid.uuid4().hex[:16]}"
        self.admin_engine = create_async_engine(DATABASE_URL)
        async with self.admin_engine.begin() as connection:
            await connection.execute(
                text(f'CREATE SCHEMA "{self.schema}"')
            )

        self.engine = create_async_engine(
            DATABASE_URL,
            connect_args={
                "server_settings": {"search_path": self.schema}
            },
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )

        async def override_get_db():
            async with self.session_factory() as session:
                yield session

        main.app.dependency_overrides[get_db] = override_get_db

        self.sent_emails = []

        async def fake_send(email: str, otp: str, name: str = "") -> None:
            self.sent_emails.append({"email": email, "otp": otp, "name": name})

        self.original_send = main.send_otp_email
        self.original_admin_emails = set(main.ADMIN_EMAILS)
        self.original_cooldown = main.OTP_RESEND_COOLDOWN_SECONDS
        self.original_hmac = main.OTP_HMAC_SECRET
        main.send_otp_email = fake_send
        main.ADMIN_EMAILS = {"admin@example.com"}
        main.OTP_RESEND_COOLDOWN_SECONDS = 0
        main.OTP_HMAC_SECRET = "test-hmac-secret"
        main.rate_limiter.reset()

        self.client = AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        main.app.dependency_overrides.clear()
        main.send_otp_email = self.original_send
        main.ADMIN_EMAILS = self.original_admin_emails
        main.OTP_RESEND_COOLDOWN_SECONDS = self.original_cooldown
        main.OTP_HMAC_SECRET = self.original_hmac
        main.rate_limiter.reset()

        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(
                text(f'DROP SCHEMA "{self.schema}" CASCADE')
            )
        await self.admin_engine.dispose()

    async def signup(self, email: str, password: str = "Password123!"):
        return await self.client.post(
            "/auth/signup",
            json={
                "name": "Test User",
                "email": email,
                "password": password,
            },
        )

    async def login(self, email: str, password: str = "Password123!"):
        return await self.client.post(
            "/auth/login",
            json={"email": email, "password": password},
        )

    async def verify(self, challenge_id: str, otp: str):
        return await self.client.post(
            "/auth/verify-otp",
            json={"challenge_id": challenge_id, "otp": otp},
        )

    def latest_otp(self, email: str) -> str:
        for sent in reversed(self.sent_emails):
            if sent["email"] == email:
                return sent["otp"]
        raise AssertionError(f"No OTP was sent to {email}")

    async def get_user(self, email: str):
        async with self.session_factory() as session:
            result = await session.execute(
                select(User).where(User.email == email)
            )
            return result.scalar_one_or_none()

    async def count_users(self, email: str) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                select(func.count(User.id)).where(User.email == email)
            )
            return int(result.scalar_one())

    async def verify_with_otp(self, email: str):
        signup_response = await self.signup(email)
        challenge_id = signup_response.json()["challenge_id"]
        return await self.verify(challenge_id, self.latest_otp(email))

    async def test_signup_creates_unverified_account_and_sends_otp(self):
        email = "new-user@example.com"
        response = await self.signup(email)

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("challenge_id", body)
        self.assertNotIn("access_token", body)
        self.assertEqual(len(self.sent_emails), 1)
        self.assertEqual(self.sent_emails[0]["email"], email)

        user = await self.get_user(email)
        self.assertIsNotNone(user)
        self.assertFalse(user.email_verified)

    async def test_otp_challenge_expires_in_ten_minutes(self):
        email = "ten-minutes@example.com"
        signup_response = await self.signup(email)
        challenge_id = signup_response.json()["challenge_id"]

        async with self.session_factory() as session:
            result = await session.execute(
                select(OTPChallenge).where(
                    OTPChallenge.challenge_id == challenge_id
                )
            )
            challenge = result.scalar_one()
            created_at = challenge.created_at
            expires_at = challenge.expires_at

        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        self.assertAlmostEqual(
            (expires_at - created_at).total_seconds(),
            600,
            delta=5,
        )

    async def test_otp_is_stored_as_hmac_not_plaintext(self):
        email = "hmac@example.com"
        signup_response = await self.signup(email)
        challenge_id = signup_response.json()["challenge_id"]
        otp = self.latest_otp(email)

        async with self.session_factory() as session:
            result = await session.execute(
                select(OTPChallenge).where(
                    OTPChallenge.challenge_id == challenge_id
                )
            )
            challenge = result.scalar_one()

        self.assertNotEqual(challenge.otp_hash, otp)
        self.assertEqual(
            challenge.otp_hash,
            main.otp_digest(challenge_id, otp),
        )

        main.OTP_HMAC_SECRET = "a-different-secret"
        self.assertNotEqual(
            challenge.otp_hash,
            main.otp_digest(challenge_id, otp),
        )

    async def test_signup_without_hmac_secret_is_refused(self):
        main.OTP_HMAC_SECRET = ""
        response = await self.signup("no-secret@example.com")
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(await self.get_user("no-secret@example.com"))
        self.assertEqual(self.sent_emails, [])

    async def test_unverified_duplicate_signup_reuses_account(self):
        email = "reuse@example.com"
        first = await self.signup(email, "Password123!")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(len(self.sent_emails), 1)

        second = await self.signup(email, "NewPassword123!")
        self.assertEqual(second.status_code, 201)
        self.assertIn("challenge_id", second.json())
        self.assertEqual(await self.count_users(email), 1)
        self.assertEqual(len(self.sent_emails), 2)

        # The refreshed password is the one that works after verification.
        async with self.session_factory() as session:
            result = await session.execute(
                select(User).where(User.email == email)
            )
            user = result.scalar_one()
        self.assertTrue(
            main.verify_password("NewPassword123!", user.password_hash)
        )
        self.assertFalse(
            main.verify_password("Password123!", user.password_hash)
        )

    async def test_verified_duplicate_signup_is_generic_and_sends_nothing(self):
        email = "verified-duplicate@example.com"
        verified = await self.verify_with_otp(email)
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(len(self.sent_emails), 1)

        duplicate = await self.signup(email)
        self.assertEqual(duplicate.status_code, 200)
        self.assertNotIn("challenge_id", duplicate.json())
        self.assertEqual(len(self.sent_emails), 1)
        self.assertEqual(await self.count_users(email), 1)

    async def test_invalid_signup_data_is_rejected(self):
        short_password = await self.client.post(
            "/auth/signup",
            json={
                "name": "Test User",
                "email": "invalid@example.com",
                "password": "short",
            },
        )
        self.assertEqual(short_password.status_code, 422)

        blank_name = await self.client.post(
            "/auth/signup",
            json={
                "name": "   ",
                "email": "invalid-name@example.com",
                "password": "Password123!",
            },
        )
        self.assertEqual(blank_name.status_code, 400)
        self.assertEqual(self.sent_emails, [])

    async def test_correct_otp_verifies_account_without_issuing_jwt(self):
        email = "verify@example.com"
        response = await self.verify_with_otp(email)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("access_token", body)
        self.assertTrue(body["user"]["email_verified"])

        user = await self.get_user(email)
        self.assertTrue(user.email_verified)

    async def test_wrong_otp_does_not_verify_account(self):
        email = "wrong-otp@example.com"
        signup_response = await self.signup(email)
        challenge_id = signup_response.json()["challenge_id"]

        response = await self.verify(
            challenge_id,
            _wrong_otp(self.latest_otp(email)),
        )
        self.assertEqual(response.status_code, 400)

        user = await self.get_user(email)
        self.assertFalse(user.email_verified)

    async def test_expired_otp_does_not_verify_account(self):
        email = "expired@example.com"
        signup_response = await self.signup(email)
        challenge_id = signup_response.json()["challenge_id"]

        async with self.session_factory() as session:
            result = await session.execute(
                select(OTPChallenge).where(
                    OTPChallenge.challenge_id == challenge_id
                )
            )
            challenge = result.scalar_one()
            challenge.expires_at = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            )
            await session.commit()

        response = await self.verify(
            challenge_id,
            self.latest_otp(email),
        )
        self.assertEqual(response.status_code, 400)

        user = await self.get_user(email)
        self.assertFalse(user.email_verified)

    async def test_reused_otp_is_rejected(self):
        email = "reused@example.com"
        signup_response = await self.signup(email)
        challenge_id = signup_response.json()["challenge_id"]
        otp = self.latest_otp(email)

        first = await self.verify(challenge_id, otp)
        self.assertEqual(first.status_code, 200)

        reuse = await self.verify(challenge_id, otp)
        self.assertEqual(reuse.status_code, 400)

    async def test_excessive_attempts_are_rejected(self):
        email = "attempts@example.com"
        signup_response = await self.signup(email)
        challenge_id = signup_response.json()["challenge_id"]
        otp = self.latest_otp(email)
        wrong = _wrong_otp(otp)

        for attempt in range(4):
            response = await self.verify(challenge_id, wrong)
            self.assertEqual(response.status_code, 400, attempt)

        fifth = await self.verify(challenge_id, wrong)
        self.assertEqual(fifth.status_code, 429)

        after_exhaustion = await self.verify(challenge_id, otp)
        self.assertEqual(after_exhaustion.status_code, 400)

        user = await self.get_user(email)
        self.assertFalse(user.email_verified)

    async def test_resend_rotates_challenge_and_enforces_cooldown(self):
        email = "resend@example.com"
        signup_response = await self.signup(email)
        original_challenge = signup_response.json()["challenge_id"]
        original_otp = self.latest_otp(email)

        main.OTP_RESEND_COOLDOWN_SECONDS = 30
        throttled = await self.client.post(
            "/auth/resend-otp",
            json={"challenge_id": original_challenge},
        )
        self.assertEqual(throttled.status_code, 429)

        main.OTP_RESEND_COOLDOWN_SECONDS = 0
        resend = await self.client.post(
            "/auth/resend-otp",
            json={"challenge_id": original_challenge},
        )
        self.assertEqual(resend.status_code, 200)
        new_challenge = resend.json()["challenge_id"]
        self.assertNotEqual(new_challenge, original_challenge)
        self.assertEqual(len(self.sent_emails), 2)
        self.assertEqual(await self.count_users(email), 1)

        user = await self.get_user(email)
        self.assertFalse(user.email_verified)

        old_result = await self.verify(original_challenge, original_otp)
        self.assertEqual(old_result.status_code, 400)

        new_result = await self.verify(
            new_challenge,
            self.latest_otp(email),
        )
        self.assertEqual(new_result.status_code, 200)

    def assert_generic_failure(self, response):
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertNotIn("challenge_id", body)
        self.assertEqual(
            body["detail"],
            "Could not send verification email, try again",
        )

    async def test_send_failure_keeps_account_unverified(self):
        email = "send-failure@example.com"

        async def failing_send(email: str, otp: str, name: str = "") -> None:
            raise EmailSendError("network")

        main.send_otp_email = failing_send

        response = await self.signup(email)
        self.assert_generic_failure(response)

        user = await self.get_user(email)
        self.assertIsNotNone(user)
        self.assertFalse(user.email_verified)
        self.assertEqual(await self.count_users(email), 1)

    async def test_send_failure_returns_one_generic_response_per_category(self):
        categories = (
            "timeout",
            "auth",
            "sender",
            "network",
            "config",
            "unknown",
        )

        for index, category in enumerate(categories):
            email = f"generic-{category}-{index}@example.com"

            async def failing_send(
                to_email: str,
                otp: str,
                name: str = "",
                _category=category,
            ) -> None:
                raise EmailSendError(_category)

            main.send_otp_email = failing_send

            response = await self.signup(email)
            self.assert_generic_failure(response)

            user = await self.get_user(email)
            self.assertIsNotNone(user)
            self.assertFalse(user.email_verified)

    async def test_signup_retry_after_failure_succeeds(self):
        email = "retry-after-failure@example.com"

        async def failing_send(
            email: str,
            otp: str,
            name: str = "",
        ) -> None:
            raise EmailSendError("network")

        main.send_otp_email = failing_send
        first = await self.signup(email)
        self.assert_generic_failure(first)

        async def recovering_send(
            email: str,
            otp: str,
            name: str = "",
        ) -> None:
            self.sent_emails.append({"email": email, "otp": otp})

        main.send_otp_email = recovering_send
        retry = await self.signup(email)
        self.assertEqual(retry.status_code, 201)
        self.assertIn("challenge_id", retry.json())
        self.assertEqual(await self.count_users(email), 1)

    async def test_failed_resend_preserves_previous_valid_challenge(self):
        email = "resend-failure@example.com"
        signup_response = await self.signup(email)
        original_challenge = signup_response.json()["challenge_id"]
        original_otp = self.latest_otp(email)
        self.assertEqual(len(self.sent_emails), 1)

        async def failing_send(email: str, otp: str, name: str = "") -> None:
            raise EmailSendError("network")

        main.send_otp_email = failing_send

        resend = await self.client.post(
            "/auth/resend-otp",
            json={"challenge_id": original_challenge},
        )
        self.assertEqual(resend.status_code, 503)
        self.assertNotIn("challenge_id", resend.json())
        self.assertEqual(len(self.sent_emails), 1)

        old_result = await self.verify(original_challenge, original_otp)
        self.assertEqual(old_result.status_code, 200)

        user = await self.get_user(email)
        self.assertTrue(user.email_verified)

    async def test_unverified_login_returns_generic_403(self):
        email = "unverified-login@example.com"
        signup_response = await self.signup(email)
        self.assertEqual(signup_response.status_code, 201)
        self.assertEqual(len(self.sent_emails), 1)

        login_response = await self.login(email)
        self.assertEqual(login_response.status_code, 403)
        self.assertNotIn("access_token", login_response.json())
        detail = login_response.json()["detail"].lower()
        self.assertNotIn("verif", detail.replace("verify your email", ""))
        self.assertEqual(len(self.sent_emails), 1)

        user = await self.get_user(email)
        self.assertFalse(user.email_verified)

    async def test_wrong_password_is_denied_without_sending_otp(self):
        email = "wrong-password@example.com"
        await self.signup(email)
        self.assertEqual(len(self.sent_emails), 1)

        response = await self.login(email, "WrongPassword1!")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.sent_emails), 1)

    async def test_verified_account_logs_in_without_otp(self):
        email = "verified-login@example.com"
        verified = await self.verify_with_otp(email)
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(len(self.sent_emails), 1)

        login_response = await self.login(email)
        self.assertEqual(login_response.status_code, 200)
        body = login_response.json()
        self.assertTrue(body["access_token"])
        self.assertEqual(body["user"]["email"], email)
        self.assertEqual(body["user"]["role"], "user")
        self.assertEqual(len(self.sent_emails), 1)

        me = await self.client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], email)

    async def test_concurrent_duplicate_signup_is_handled(self):
        email = "concurrent@example.com"
        responses = await asyncio.gather(
            self.signup(email),
            self.signup(email),
        )
        statuses = sorted(response.status_code for response in responses)
        self.assertEqual(await self.count_users(email), 1)
        self.assertTrue(all(code in (200, 201) for code in statuses))

    async def test_admin_and_user_authorization(self):
        admin_email = "admin@example.com"
        await self.verify_with_otp(admin_email)
        admin_login = await self.login(admin_email)
        self.assertEqual(admin_login.status_code, 200)
        admin_token = admin_login.json()["access_token"]
        self.assertEqual(admin_login.json()["user"]["role"], "admin")

        admin_check = await self.client.get(
            "/admin/check",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(admin_check.status_code, 200)

        user_email = "normal@example.com"
        await self.verify_with_otp(user_email)
        user_login = await self.login(user_email)
        self.assertEqual(user_login.status_code, 200)
        user_token = user_login.json()["access_token"]
        self.assertEqual(user_login.json()["user"]["role"], "user")

        user_admin_check = await self.client.get(
            "/admin/check",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        self.assertEqual(user_admin_check.status_code, 403)

    async def test_email_send_error_never_leaks_secrets(self):
        async def failing_send(email: str, otp: str) -> None:
            raise EmailSendError("auth")

        main.send_otp_email = failing_send

        response = await self.signup("safe-detail@example.com")
        detail = response.json()["detail"]
        self.assertNotIn("re_", detail)
        self.assertNotIn("secret", detail.lower())
        self.assertNotIn("otp", detail.lower())

    async def test_invalid_and_expired_jwts_are_rejected(self):
        invalid = await self.client.get(
            "/auth/me",
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        self.assertEqual(invalid.status_code, 401)

        now = datetime.now(timezone.utc)
        expired_token = jwt.encode(
            {
                "sub": "1",
                "email": "expired@example.com",
                "role": "user",
                "iat": now - timedelta(hours=1),
                "exp": now - timedelta(minutes=1),
            },
            main.JWT_SECRET_KEY,
            algorithm=main.JWT_ALGORITHM,
        )
        expired = await self.client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        self.assertEqual(expired.status_code, 401)


class GoogleOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = f"test_google_{uuid.uuid4().hex[:16]}"
        self.admin_engine = create_async_engine(DATABASE_URL)
        async with self.admin_engine.begin() as connection:
            await connection.execute(
                text(f'CREATE SCHEMA "{self.schema}"')
            )

        self.engine = create_async_engine(
            DATABASE_URL,
            connect_args={
                "server_settings": {"search_path": self.schema}
            },
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )

        async def override_get_db():
            async with self.session_factory() as session:
                yield session

        main.app.dependency_overrides[get_db] = override_get_db

        self.original_send = main.send_otp_email
        self.original_admin_emails = set(main.ADMIN_EMAILS)
        main.ADMIN_EMAILS = {"admin@example.com"}
        main.rate_limiter.reset()

        async def fake_send(email: str, otp: str, name: str = "") -> None:
            return None

        main.send_otp_email = fake_send

        self.client = AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        main.app.dependency_overrides.clear()
        main.send_otp_email = self.original_send
        main.ADMIN_EMAILS = self.original_admin_emails
        main.rate_limiter.reset()

        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(
                text(f'DROP SCHEMA "{self.schema}" CASCADE')
            )
        await self.admin_engine.dispose()

    async def test_google_login_creates_and_links_verified_users(self):
        google_payload = {
            "email": "google@example.com",
            "sub": "google-subject-1",
            "name": "Google User",
            "email_verified": True,
        }
        captured = {}

        def fake_verify(token, request, audience):
            captured["token"] = token
            captured["audience"] = audience
            return google_payload

        original_verify = main.id_token.verify_oauth2_token
        main.id_token.verify_oauth2_token = fake_verify

        try:
            response = await self.client.post(
                "/auth/google",
                json={"id_token": "fake-google-token"},
            )
        finally:
            main.id_token.verify_oauth2_token = original_verify

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["audience"], main.GOOGLE_CLIENT_ID)
        body = response.json()
        self.assertTrue(body["user"]["email_verified"])
        self.assertTrue(body["access_token"])

        password_email = "linkme@example.com"
        await self.client.post(
            "/auth/signup",
            json={
                "name": "Test User",
                "email": password_email,
                "password": "Password123!",
            },
        )

        google_payload = {
            "email": password_email,
            "sub": "google-subject-2",
            "name": "Linked User",
            "email_verified": True,
        }

        def fake_verify_link(token, request, audience):
            return google_payload

        main.id_token.verify_oauth2_token = fake_verify_link

        try:
            linked = await self.client.post(
                "/auth/google",
                json={"id_token": "fake-google-token-2"},
            )
        finally:
            main.id_token.verify_oauth2_token = original_verify

        self.assertEqual(linked.status_code, 200)
        self.assertTrue(linked.json()["user"]["email_verified"])

        async with self.session_factory() as session:
            result = await session.execute(
                select(User).where(User.email == password_email)
            )
            user = result.scalar_one()
            self.assertEqual(user.google_sub, "google-subject-2")
            self.assertTrue(user.email_verified)

    async def test_invalid_google_token_is_rejected(self):
        def fake_verify(token, request, audience):
            raise ValueError("bad token")

        original_verify = main.id_token.verify_oauth2_token
        main.id_token.verify_oauth2_token = fake_verify

        try:
            response = await self.client.post(
                "/auth/google",
                json={"id_token": "not-a-real-token"},
            )
        finally:
            main.id_token.verify_oauth2_token = original_verify

        self.assertEqual(response.status_code, 401)


class ConfigTests(unittest.TestCase):
    def test_backend_env_file_is_loaded_from_backend_directory(self):
        self.assertEqual(
            main.BACKEND_DIR,
            Path(main.__file__).resolve().parent,
        )

    def test_provider_config_covers_all_supported_values(self):
        for provider in ("smtp", "brevo", "resend", "gmail_api"):
            with patch.dict("os.environ", {"EMAIL_PROVIDER": provider}):
                self.assertEqual(
                    email_service._env("EMAIL_PROVIDER"),
                    provider,
                )


class AuthorizeGmailTests(unittest.TestCase):
    def test_token_error_reports_status_and_oauth_fields(self):
        response = httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Malformed auth code.",
            },
            request=httpx.Request("POST", authorize_gmail.TOKEN_URL),
        )

        message = authorize_gmail._describe_token_error(response)

        self.assertIn("HTTP 400", message)
        self.assertIn("error=invalid_grant", message)
        self.assertIn("error_description=Malformed auth code.", message)

    def test_token_error_handles_non_json_body_safely(self):
        response = httpx.Response(
            401,
            content=b"<html>unauthorized</html>",
            request=httpx.Request("POST", authorize_gmail.TOKEN_URL),
        )

        message = authorize_gmail._describe_token_error(response)

        self.assertEqual(message, "HTTP 401")

    def test_token_error_never_leaks_secrets_or_tokens(self):
        response = httpx.Response(
            401,
            json={
                "error": "unauthorized_client",
                "error_description": "The OAuth client was not found.",
            },
            request=httpx.Request("POST", authorize_gmail.TOKEN_URL),
        )

        message = authorize_gmail._describe_token_error(response)

        for secret in (
            "client-secret-value",
            "authorization-code-value",
            "ya29.access-token-value",
            "1//refresh-token-value",
        ):
            self.assertNotIn(secret, message)


class CheckEmailOAuthTests(unittest.TestCase):
    def test_oauth_error_reports_status_and_fields(self):
        response = httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )

        message = check_email._describe_oauth_error(response)

        self.assertIn("HTTP 400", message)
        self.assertIn("error=invalid_grant", message)
        self.assertIn(
            "error_description=Token has been expired or revoked.", message
        )

    def test_oauth_error_reports_invalid_client(self):
        response = httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "Unauthorized",
            },
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )

        message = check_email._describe_oauth_error(response)

        self.assertIn("HTTP 401", message)
        self.assertIn("error=invalid_client", message)

    def test_oauth_error_handles_non_json_body_safely(self):
        response = httpx.Response(
            500,
            content=b"<html>server error</html>",
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )

        message = check_email._describe_oauth_error(response)

        self.assertEqual(message, "HTTP 500")

    def test_oauth_error_never_leaks_secrets_or_tokens(self):
        response = httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "Unauthorized",
                "client_secret": "client-secret-value",
                "refresh_token": "1//refresh-token-value",
                "access_token": "ya29.access-token-value",
            },
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )

        message = check_email._describe_oauth_error(response)

        for secret in (
            "client-secret-value",
            "1//refresh-token-value",
            "ya29.access-token-value",
        ):
            self.assertNotIn(secret, message)


if __name__ == "__main__":
    asyncio.run(unittest.main())

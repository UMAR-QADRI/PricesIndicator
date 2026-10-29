import unittest
import uuid

from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

import main
from database import DATABASE_URL, Base, get_db
from models import PasswordResetChallenge, User

PASSWORD = "Password123!"
NEW_PASSWORD = "NewPassword456!"


def _wrong_otp(correct: str) -> str:
    return "000001" if correct == "000000" else "000000"


class PasswordResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = f"test_reset_{uuid.uuid4().hex[:16]}"
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
        self.sent_reset_emails = []

        async def fake_send(
            email: str,
            otp: str,
            name: str = "",
        ) -> None:
            self.sent_emails.append(
                {"email": email, "otp": otp, "name": name}
            )

        async def fake_reset_send(
            email: str,
            otp: str,
            name: str = "",
        ) -> None:
            self.sent_reset_emails.append(
                {"email": email, "otp": otp, "name": name}
            )

        self.original_send = main.send_otp_email
        self.original_reset_send = main.send_password_reset_email
        self.original_admin_emails = set(main.ADMIN_EMAILS)
        self.original_cooldown = main.OTP_RESEND_COOLDOWN_SECONDS
        self.original_hmac = main.OTP_HMAC_SECRET
        self.original_limits = (
            main.RATE_LIMIT_SIGNUP,
            main.RATE_LIMIT_LOGIN,
            main.RATE_LIMIT_VERIFY,
            main.RATE_LIMIT_RESEND,
            main.RATE_LIMIT_FORGOT,
            main.RATE_LIMIT_RESET,
        )

        main.send_otp_email = fake_send
        main.send_password_reset_email = fake_reset_send
        main.ADMIN_EMAILS = {"admin@example.com"}
        main.OTP_RESEND_COOLDOWN_SECONDS = 0
        main.OTP_HMAC_SECRET = "test-password-reset-hmac-secret"
        main.RATE_LIMIT_SIGNUP = (1000, 60)
        main.RATE_LIMIT_LOGIN = (1000, 60)
        main.RATE_LIMIT_VERIFY = (1000, 60)
        main.RATE_LIMIT_RESEND = (1000, 60)
        main.RATE_LIMIT_FORGOT = (1000, 60)
        main.RATE_LIMIT_RESET = (1000, 60)
        main.rate_limiter.reset()

        self.client = AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        main.app.dependency_overrides.clear()
        main.send_otp_email = self.original_send
        main.send_password_reset_email = self.original_reset_send
        main.ADMIN_EMAILS = self.original_admin_emails
        main.OTP_RESEND_COOLDOWN_SECONDS = self.original_cooldown
        main.OTP_HMAC_SECRET = self.original_hmac
        (
            main.RATE_LIMIT_SIGNUP,
            main.RATE_LIMIT_LOGIN,
            main.RATE_LIMIT_VERIFY,
            main.RATE_LIMIT_RESEND,
            main.RATE_LIMIT_FORGOT,
            main.RATE_LIMIT_RESET,
        ) = self.original_limits
        main.rate_limiter.reset()

        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(
                text(f'DROP SCHEMA "{self.schema}" CASCADE')
            )
        await self.admin_engine.dispose()

    async def signup(
        self,
        email: str,
        password: str = PASSWORD,
    ):
        return await self.client.post(
            "/auth/signup",
            json={
                "name": "Test User",
                "email": email,
                "password": password,
            },
        )

    async def login(
        self,
        email: str,
        password: str = PASSWORD,
    ):
        return await self.client.post(
            "/auth/login",
            json={"email": email, "password": password},
        )

    async def forgot(self, email: str):
        return await self.client.post(
            "/auth/forgot-password",
            json={"email": email},
        )

    async def verify_reset(self, challenge_id: str, otp: str):
        return await self.client.post(
            "/auth/verify-password-reset-otp",
            json={"challenge_id": challenge_id, "otp": otp},
        )

    async def reset(self, reset_token: str, password: str = NEW_PASSWORD):
        return await self.client.post(
            "/auth/reset-password",
            json={"reset_token": reset_token, "password": password},
        )

    def latest_otp(self, email: str) -> str:
        for sent in reversed(self.sent_emails):
            if sent["email"] == email:
                return sent["otp"]
        raise AssertionError(f"No OTP was sent to {email}")

    def latest_reset_otp(self, email: str) -> str:
        for sent in reversed(self.sent_reset_emails):
            if sent["email"] == email:
                return sent["otp"]
        raise AssertionError(f"No reset OTP was sent to {email}")

    async def get_user(self, email: str) -> User | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(User).where(User.email == email)
            )
            return result.scalar_one_or_none()

    async def get_challenge(
        self,
        challenge_id: str,
    ) -> PasswordResetChallenge | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(PasswordResetChallenge).where(
                    PasswordResetChallenge.challenge_id == challenge_id
                )
            )
            return result.scalar_one_or_none()

    async def create_verified_user(self, email: str) -> User:
        signup = await self.signup(email)
        self.assertEqual(signup.status_code, 201, signup.text)
        challenge_id = signup.json()["challenge_id"]

        verified = await self.client.post(
            "/auth/verify-otp",
            json={
                "challenge_id": challenge_id,
                "otp": self.latest_otp(email),
            },
        )
        self.assertEqual(verified.status_code, 200, verified.text)

        user = await self.get_user(email)
        self.assertIsNotNone(user)
        return user

    async def start_reset(self, email: str) -> tuple[str, str]:
        response = await self.forgot(email)
        self.assertEqual(response.status_code, 200, response.text)
        challenge_id = response.json()["challenge_id"]
        return challenge_id, self.latest_reset_otp(email)

    async def test_forgot_password_is_generic_for_known_and_unknown_emails(self):
        email = "known-reset@example.com"
        await self.create_verified_user(email)

        known = await self.forgot(email)
        unknown = await self.forgot("missing-reset@example.com")

        self.assertEqual(known.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(
            known.json()["message"],
            unknown.json()["message"],
        )
        self.assertIn(
            "If an account exists",
            known.json()["message"],
        )
        self.assertIn("challenge_id", known.json())
        self.assertIn("challenge_id", unknown.json())
        self.assertEqual(len(self.sent_reset_emails), 1)
        self.assertEqual(self.sent_reset_emails[0]["email"], email)

    async def test_forgot_password_does_not_email_google_only_accounts(self):
        email = "google-only-reset@example.com"

        async with self.session_factory() as session:
            session.add(
                User(
                    name="Google User",
                    email=email,
                    google_sub="google-reset-subject",
                    email_verified=True,
                )
            )
            await session.commit()

        response = await self.forgot(email)

        self.assertEqual(response.status_code, 200)
        self.assertIn("If an account exists", response.json()["message"])
        self.assertEqual(self.sent_reset_emails, [])

    async def test_forgot_password_does_not_email_blocked_accounts(self):
        email = "blocked-reset@example.com"
        user = await self.create_verified_user(email)

        async with self.session_factory() as session:
            blocked = await session.get(User, user.id)
            blocked.is_blocked = True
            await session.commit()

        response = await self.forgot(email)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sent_reset_emails, [])

    async def test_reset_otp_expires_and_cannot_be_reused(self):
        email = "expired-reset@example.com"
        await self.create_verified_user(email)
        challenge_id, otp = await self.start_reset(email)

        async with self.session_factory() as session:
            challenge = (
                await session.execute(
                    select(PasswordResetChallenge).where(
                        PasswordResetChallenge.challenge_id
                        == challenge_id
                    )
                )
            ).scalar_one()
            challenge.expires_at = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            )
            await session.commit()

        expired = await self.verify_reset(challenge_id, otp)
        self.assertEqual(expired.status_code, 400)
        self.assertNotIn("reset_token", expired.json())

        reuse = await self.verify_reset(challenge_id, otp)
        self.assertEqual(reuse.status_code, 400)
        self.assertNotIn("reset_token", reuse.json())

    async def test_reset_otp_attempt_limit(self):
        email = "attempts-reset@example.com"
        await self.create_verified_user(email)
        challenge_id, otp = await self.start_reset(email)
        wrong = _wrong_otp(otp)

        for attempt in range(4):
            response = await self.verify_reset(challenge_id, wrong)
            self.assertEqual(response.status_code, 400, attempt)

        fifth = await self.verify_reset(challenge_id, wrong)
        self.assertEqual(fifth.status_code, 429)

        after_exhaustion = await self.verify_reset(challenge_id, otp)
        self.assertEqual(after_exhaustion.status_code, 400)
        self.assertNotIn("reset_token", after_exhaustion.json())

    async def test_otp_and_reset_token_are_stored_hashed(self):
        email = "hashed-reset@example.com"
        await self.create_verified_user(email)
        challenge_id, otp = await self.start_reset(email)

        stored = await self.get_challenge(challenge_id)
        self.assertNotEqual(stored.otp_hash, otp)
        self.assertEqual(
            stored.otp_hash,
            main.otp_digest(challenge_id, otp),
        )

        verified = await self.verify_reset(challenge_id, otp)
        self.assertEqual(verified.status_code, 200, verified.text)
        reset_token = verified.json()["reset_token"]

        stored = await self.get_challenge(challenge_id)
        self.assertTrue(stored.otp_verified)
        self.assertFalse(stored.consumed)
        self.assertNotEqual(stored.reset_token_hash, reset_token)
        self.assertEqual(
            stored.reset_token_hash,
            main.reset_token_digest(reset_token),
        )

    async def test_successful_reset_updates_password_and_consumes_token(self):
        email = "successful-reset@example.com"
        user = await self.create_verified_user(email)
        challenge_id, otp = await self.start_reset(email)

        verified = await self.verify_reset(challenge_id, otp)
        self.assertEqual(verified.status_code, 200, verified.text)
        reset_token = verified.json()["reset_token"]

        reset = await self.reset(reset_token)
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertIn("log in", reset.json()["message"])

        updated = await self.get_user(email)
        self.assertNotEqual(updated.password_hash, NEW_PASSWORD)
        self.assertTrue(
            main.verify_password(NEW_PASSWORD, updated.password_hash)
        )
        self.assertFalse(
            main.verify_password(PASSWORD, updated.password_hash)
        )
        self.assertEqual(updated.id, user.id)

        old_login = await self.login(email, PASSWORD)
        self.assertEqual(old_login.status_code, 401)

        new_login = await self.login(email, NEW_PASSWORD)
        self.assertEqual(new_login.status_code, 200)
        self.assertTrue(new_login.json()["access_token"])

        reused_token = await self.reset(reset_token)
        self.assertEqual(reused_token.status_code, 400)

        reused_otp = await self.verify_reset(challenge_id, otp)
        self.assertEqual(reused_otp.status_code, 400)

        stored = await self.get_challenge(challenge_id)
        self.assertTrue(stored.consumed)
        self.assertIsNone(stored.reset_token_hash)
        self.assertIsNone(stored.reset_token_expires_at)

    async def test_unauthorized_reset_token_does_not_change_password(self):
        email = "unauthorized-reset@example.com"
        await self.create_verified_user(email)

        invalid = await self.reset("not-a-real-reset-token")
        self.assertEqual(invalid.status_code, 400)

        old_login = await self.login(email, PASSWORD)
        self.assertEqual(old_login.status_code, 200)

        new_login = await self.login(email, NEW_PASSWORD)
        self.assertEqual(new_login.status_code, 401)

    async def test_expired_reset_token_is_rejected(self):
        email = "expired-token@example.com"
        await self.create_verified_user(email)
        challenge_id, otp = await self.start_reset(email)

        verified = await self.verify_reset(challenge_id, otp)
        self.assertEqual(verified.status_code, 200, verified.text)
        reset_token = verified.json()["reset_token"]

        async with self.session_factory() as session:
            challenge = (
                await session.execute(
                    select(PasswordResetChallenge).where(
                        PasswordResetChallenge.challenge_id
                        == challenge_id
                    )
                )
            ).scalar_one()
            challenge.reset_token_expires_at = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            )
            await session.commit()

        expired = await self.reset(reset_token)
        self.assertEqual(expired.status_code, 400)

        old_login = await self.login(email, PASSWORD)
        self.assertEqual(old_login.status_code, 200)

        new_login = await self.login(email, NEW_PASSWORD)
        self.assertEqual(new_login.status_code, 401)

    async def test_resend_rotates_challenge_and_enforces_cooldown(self):
        email = "resend-reset@example.com"
        await self.create_verified_user(email)
        original_challenge, original_otp = await self.start_reset(email)

        main.OTP_RESEND_COOLDOWN_SECONDS = 30
        throttled = await self.client.post(
            "/auth/resend-password-reset",
            json={"challenge_id": original_challenge},
        )
        self.assertEqual(throttled.status_code, 429)

        main.OTP_RESEND_COOLDOWN_SECONDS = 0
        resend = await self.client.post(
            "/auth/resend-password-reset",
            json={"challenge_id": original_challenge},
        )
        self.assertEqual(resend.status_code, 200, resend.text)
        new_challenge = resend.json()["challenge_id"]
        self.assertNotEqual(new_challenge, original_challenge)
        self.assertEqual(len(self.sent_reset_emails), 2)

        old_result = await self.verify_reset(
            original_challenge,
            original_otp,
        )
        self.assertEqual(old_result.status_code, 400)

        new_result = await self.verify_reset(
            new_challenge,
            self.latest_reset_otp(email),
        )
        self.assertEqual(new_result.status_code, 200, new_result.text)
        self.assertTrue(new_result.json()["reset_token"])

    async def test_password_rules_are_enforced_before_token_is_consumed(self):
        email = "rules-reset@example.com"
        await self.create_verified_user(email)
        challenge_id, otp = await self.start_reset(email)

        verified = await self.verify_reset(challenge_id, otp)
        self.assertEqual(verified.status_code, 200, verified.text)
        reset_token = verified.json()["reset_token"]

        too_short = await self.reset(reset_token, "short")
        self.assertEqual(too_short.status_code, 422)

        too_long_for_bcrypt = await self.reset(reset_token, "a" * 73)
        self.assertEqual(too_long_for_bcrypt.status_code, 400)

        old_login = await self.login(email, PASSWORD)
        self.assertEqual(old_login.status_code, 200)

        success = await self.reset(reset_token)
        self.assertEqual(success.status_code, 200, success.text)

        new_login = await self.login(email, NEW_PASSWORD)
        self.assertEqual(new_login.status_code, 200)

    async def test_blocked_user_cannot_finish_reset_after_token_issued(self):
        email = "blocked-token@example.com"
        user = await self.create_verified_user(email)
        challenge_id, otp = await self.start_reset(email)

        verified = await self.verify_reset(challenge_id, otp)
        self.assertEqual(verified.status_code, 200, verified.text)
        reset_token = verified.json()["reset_token"]

        async with self.session_factory() as session:
            blocked = await session.get(User, user.id)
            blocked.is_blocked = True
            await session.commit()

        reset = await self.reset(reset_token)
        self.assertEqual(reset.status_code, 400)

        unchanged = await self.get_user(email)
        self.assertTrue(
            main.verify_password(PASSWORD, unchanged.password_hash)
        )
        self.assertFalse(
            main.verify_password(NEW_PASSWORD, unchanged.password_hash)
        )


if __name__ == "__main__":
    unittest.main()

import unittest
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

import main
from database import DATABASE_URL, Base, get_db
from models import LoginEvent, OTPChallenge, User

PASSWORD = "Password123!"
ADMIN_EMAIL = "admin@example.com"


class AdminUserManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = f"test_admin_{uuid.uuid4().hex[:16]}"
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

        async def fake_send(
            email: str,
            otp: str,
            name: str = "",
        ) -> None:
            self.sent_emails.append(
                {"email": email, "otp": otp, "name": name}
            )

        self.original_send = main.send_otp_email
        self.original_admin_emails = set(main.ADMIN_EMAILS)
        self.original_cooldown = main.OTP_RESEND_COOLDOWN_SECONDS
        self.original_hmac = main.OTP_HMAC_SECRET
        self.original_google_client_id = main.GOOGLE_CLIENT_ID
        self.original_limits = (
            main.RATE_LIMIT_SIGNUP,
            main.RATE_LIMIT_LOGIN,
            main.RATE_LIMIT_VERIFY,
            main.RATE_LIMIT_RESEND,
        )

        main.send_otp_email = fake_send
        main.ADMIN_EMAILS = {ADMIN_EMAIL}
        main.OTP_RESEND_COOLDOWN_SECONDS = 0
        main.OTP_HMAC_SECRET = "test-admin-hmac-secret"
        main.GOOGLE_CLIENT_ID = "test-google-client-id"
        main.RATE_LIMIT_SIGNUP = (1000, 60)
        main.RATE_LIMIT_LOGIN = (1000, 60)
        main.RATE_LIMIT_VERIFY = (1000, 60)
        main.RATE_LIMIT_RESEND = (1000, 60)
        main.rate_limiter.reset()

        self.client = AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        )
        self._admin_token: str | None = None

    async def asyncTearDown(self):
        await self.client.aclose()
        main.app.dependency_overrides.clear()
        main.send_otp_email = self.original_send
        main.ADMIN_EMAILS = self.original_admin_emails
        main.OTP_RESEND_COOLDOWN_SECONDS = self.original_cooldown
        main.OTP_HMAC_SECRET = self.original_hmac
        main.GOOGLE_CLIENT_ID = self.original_google_client_id
        (
            main.RATE_LIMIT_SIGNUP,
            main.RATE_LIMIT_LOGIN,
            main.RATE_LIMIT_VERIFY,
            main.RATE_LIMIT_RESEND,
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
        name: str = "Test User",
        password: str = PASSWORD,
    ):
        return await self.client.post(
            "/auth/signup",
            json={"name": name, "email": email, "password": password},
        )

    async def login(self, email: str, password: str = PASSWORD):
        return await self.client.post(
            "/auth/login",
            json={"email": email, "password": password},
        )

    def latest_otp(self, email: str) -> str:
        for sent in reversed(self.sent_emails):
            if sent["email"] == email:
                return sent["otp"]
        raise AssertionError(f"No OTP was sent to {email}")

    async def verify_email(self, email: str):
        response = await self.signup(email)

        if response.status_code != 201:
            return response

        challenge_id = response.json()["challenge_id"]

        return await self.client.post(
            "/auth/verify-otp",
            json={
                "challenge_id": challenge_id,
                "otp": self.latest_otp(email),
            },
        )

    async def create_verified_user(
        self,
        email: str,
        name: str = "Test User",
    ) -> User:
        response = await self.verify_email(email)
        self.assertEqual(response.status_code, 200, response.text)
        user = await self.get_user(email)
        self.assertIsNotNone(user)
        return user

    async def get_user(self, email: str) -> User | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(User).where(User.email == email)
            )
            return result.scalar_one_or_none()

    async def admin_headers(self) -> dict:
        if self._admin_token is None:
            await self.create_verified_user(
                ADMIN_EMAIL,
                name="Admin User",
            )
            response = await self.login(ADMIN_EMAIL)
            self.assertEqual(response.status_code, 200, response.text)
            self._admin_token = response.json()["access_token"]

        return {
            "Authorization": f"Bearer {self._admin_token}",
        }

    async def user_headers(self, email: str) -> dict:
        response = await self.login(email)
        self.assertEqual(response.status_code, 200, response.text)
        return {
            "Authorization": f"Bearer {response.json()['access_token']}",
        }

    async def count_rows(self, model) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(model)
            )
            return int(result.scalar_one())

    async def test_admin_list_excludes_sensitive_fields(self):
        admin_headers = await self.admin_headers()
        await self.create_verified_user("member@example.com")
        login = await self.login("member@example.com")
        self.assertEqual(login.status_code, 200, login.text)

        response = await self.client.get(
            "/admin/users",
            headers=admin_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)

        body = response.json()
        member = next(
            item
            for item in body["items"]
            if item["email"] == "member@example.com"
        )

        self.assertEqual(member["role"], "user")
        self.assertEqual(member["status"], "active")
        self.assertEqual(member["auth_provider"], "password")
        self.assertIsNotNone(member["last_login_at"])
        self.assertEqual(member["last_login_method"], "password")

        for forbidden in (
            "password_hash",
            "google_sub",
            "otp_hash",
            "access_token",
        ):
            self.assertNotIn(forbidden, member)
            self.assertNotIn(forbidden, body)

    async def test_search_status_filter_and_pagination(self):
        admin_headers = await self.admin_headers()
        await self.create_verified_user(
            "alpha@example.com",
            name="Alpha User",
        )
        await self.create_verified_user(
            "bravo@example.com",
            name="Bravo User",
        )
        await self.signup("inactive@example.com", name="Inactive User")

        search = await self.client.get(
            "/admin/users",
            headers=admin_headers,
            params={"search": "alpha", "page_size": 1},
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertEqual(search.json()["total"], 1)
        self.assertEqual(
            search.json()["items"][0]["email"],
            "alpha@example.com",
        )

        by_id = await self.client.get(
            "/admin/users",
            headers=admin_headers,
            params={"search": search.json()["items"][0]["id"]},
        )
        self.assertEqual(by_id.status_code, 200, by_id.text)
        self.assertEqual(by_id.json()["total"], 1)

        inactive = await self.client.get(
            "/admin/users",
            headers=admin_headers,
            params={"status": "inactive"},
        )
        self.assertEqual(inactive.status_code, 200, inactive.text)
        self.assertEqual(
            [item["email"] for item in inactive.json()["items"]],
            ["inactive@example.com"],
        )
        self.assertEqual(inactive.json()["items"][0]["status"], "inactive")

        paged = await self.client.get(
            "/admin/users",
            headers=admin_headers,
            params={"page": 1, "page_size": 2},
        )
        self.assertEqual(paged.status_code, 200, paged.text)
        self.assertEqual(len(paged.json()["items"]), 2)
        self.assertGreaterEqual(paged.json()["pages"], 2)

    async def test_admin_endpoints_require_admin(self):
        admin_headers = await self.admin_headers()
        user = await self.create_verified_user("normal@example.com")
        user_headers = await self.user_headers(user.email)
        admin = await self.get_user(ADMIN_EMAIL)
        self.assertIsNotNone(admin)

        unauthenticated = await self.client.get("/admin/users")
        self.assertEqual(unauthenticated.status_code, 401)

        list_as_user = await self.client.get(
            "/admin/users",
            headers=user_headers,
        )
        self.assertEqual(list_as_user.status_code, 403)

        edit_as_user = await self.client.patch(
            f"/admin/users/{user.id}",
            headers=user_headers,
            json={"name": "Escalated User"},
        )
        self.assertEqual(edit_as_user.status_code, 403)

        block_admin_as_user = await self.client.post(
            f"/admin/users/{admin.id}/block",
            headers=user_headers,
        )
        self.assertEqual(block_admin_as_user.status_code, 403)

        delete_admin_as_user = await self.client.delete(
            f"/admin/users/{admin.id}",
            headers=user_headers,
        )
        self.assertEqual(delete_admin_as_user.status_code, 403)

        self.assertEqual(
            (await self.client.get(
                "/admin/users",
                headers=admin_headers,
            )).status_code,
            200,
        )

    async def test_edit_user_name_email_duplicate_and_self_protection(self):
        admin_headers = await self.admin_headers()
        user = await self.create_verified_user("editme@example.com")
        other = await self.create_verified_user("other@example.com")
        admin = await self.get_user(ADMIN_EMAIL)
        self.assertIsNotNone(admin)
        self.assertIsNotNone(user)
        self.assertIsNotNone(other)

        renamed = await self.client.patch(
            f"/admin/users/{user.id}",
            headers=admin_headers,
            json={"name": "Updated Name"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "Updated Name")

        duplicate = await self.client.patch(
            f"/admin/users/{user.id}",
            headers=admin_headers,
            json={"email": other.email},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

        invalid = await self.client.patch(
            f"/admin/users/{user.id}",
            headers=admin_headers,
            json={"email": "not-an-email"},
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)

        changed = await self.client.patch(
            f"/admin/users/{user.id}",
            headers=admin_headers,
            json={"email": "changed@example.com"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["email"], "changed@example.com")
        self.assertFalse(changed.json()["email_verified"])
        self.assertEqual(changed.json()["status"], "inactive")

        refreshed = await self.get_user("changed@example.com")
        self.assertIsNotNone(refreshed)
        self.assertFalse(refreshed.email_verified)
        self.assertIsNone(refreshed.google_sub)

        old_login = await self.login("editme@example.com")
        self.assertEqual(old_login.status_code, 401)

        unverified_login = await self.login("changed@example.com")
        self.assertEqual(unverified_login.status_code, 403)

        self_change = await self.client.patch(
            f"/admin/users/{admin.id}",
            headers=admin_headers,
            json={"email": "admin-changed@example.com"},
        )
        self.assertEqual(self_change.status_code, 400, self_change.text)

        self_role = await self.client.patch(
            f"/admin/users/{admin.id}",
            headers=admin_headers,
            json={"role": "user"},
        )
        self.assertEqual(self_role.status_code, 400, self_role.text)

    async def test_role_management_and_allowlist_demotion_protection(self):
        admin_headers = await self.admin_headers()
        user = await self.create_verified_user("promoted@example.com")
        user_headers = await self.user_headers(user.email)

        promoted = await self.client.patch(
            f"/admin/users/{user.id}",
            headers=admin_headers,
            json={"role": "admin"},
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(promoted.json()["role"], "admin")

        promoted_check = await self.client.get(
            "/admin/check",
            headers=user_headers,
        )
        self.assertEqual(promoted_check.status_code, 200, promoted_check.text)

        demoted = await self.client.patch(
            f"/admin/users/{user.id}",
            headers=admin_headers,
            json={"role": "user"},
        )
        self.assertEqual(demoted.status_code, 200, demoted.text)
        self.assertEqual(demoted.json()["role"], "user")

        demoted_check = await self.client.get(
            "/admin/check",
            headers=user_headers,
        )
        self.assertEqual(demoted_check.status_code, 403)

        second_admin = await self.create_verified_user(
            "second-admin@example.com"
        )
        second_headers = await self.user_headers(second_admin.email)
        second_promote = await self.client.patch(
            f"/admin/users/{second_admin.id}",
            headers=admin_headers,
            json={"role": "admin"},
        )
        self.assertEqual(second_promote.status_code, 200, second_promote.text)

        allowlist_demote = await self.client.patch(
            f"/admin/users/{(await self.get_user(ADMIN_EMAIL)).id}",
            headers=second_headers,
            json={"role": "user"},
        )
        self.assertEqual(allowlist_demote.status_code, 409, allowlist_demote.text)

    async def test_block_unblock_enforces_existing_tokens_and_otp(self):
        admin_headers = await self.admin_headers()
        user = await self.create_verified_user("blocked@example.com")
        user_headers = await self.user_headers(user.email)
        admin = await self.get_user(ADMIN_EMAIL)
        self.assertIsNotNone(admin)

        me_before = await self.client.get(
            "/auth/me",
            headers=user_headers,
        )
        self.assertEqual(me_before.status_code, 200)

        blocked = await self.client.post(
            f"/admin/users/{user.id}/block",
            headers=admin_headers,
        )
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertEqual(blocked.json()["status"], "blocked")

        me_after = await self.client.get(
            "/auth/me",
            headers=user_headers,
        )
        self.assertEqual(me_after.status_code, 403)

        blocked_login = await self.login(user.email)
        self.assertEqual(blocked_login.status_code, 403)

        self_block = await self.client.post(
            f"/admin/users/{admin.id}/block",
            headers=admin_headers,
        )
        self.assertEqual(self_block.status_code, 400, self_block.text)

        pending = await self.signup("pending@example.com")
        self.assertEqual(pending.status_code, 201, pending.text)
        challenge_id = pending.json()["challenge_id"]
        pending_user = await self.get_user("pending@example.com")
        self.assertIsNotNone(pending_user)

        pending_block = await self.client.post(
            f"/admin/users/{pending_user.id}/block",
            headers=admin_headers,
        )
        self.assertEqual(pending_block.status_code, 200, pending_block.text)

        blocked_resend = await self.client.post(
            "/auth/resend-otp",
            json={"challenge_id": challenge_id},
        )
        self.assertEqual(blocked_resend.status_code, 403)

        blocked_verify = await self.client.post(
            "/auth/verify-otp",
            json={
                "challenge_id": challenge_id,
                "otp": self.latest_otp("pending@example.com"),
            },
        )
        self.assertEqual(blocked_verify.status_code, 403)

        unblocked = await self.client.post(
            f"/admin/users/{user.id}/unblock",
            headers=admin_headers,
        )
        self.assertEqual(unblocked.status_code, 200, unblocked.text)
        self.assertEqual(unblocked.json()["status"], "active")

        login_after_unblock = await self.login(user.email)
        self.assertEqual(login_after_unblock.status_code, 200)

        me_again = await self.client.get(
            "/auth/me",
            headers=user_headers,
        )
        self.assertEqual(me_again.status_code, 200)

    async def test_delete_cascades_and_protects_self(self):
        admin_headers = await self.admin_headers()
        user = await self.create_verified_user("delete-me@example.com")
        login = await self.login(user.email)
        self.assertEqual(login.status_code, 200)

        admin = await self.get_user(ADMIN_EMAIL)
        self.assertIsNotNone(admin)
        self.assertGreater(await self.count_rows(LoginEvent), 0)
        self.assertGreater(await self.count_rows(OTPChallenge), 0)

        deleted = await self.client.delete(
            f"/admin/users/{user.id}",
            headers=admin_headers,
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertIsNone(await self.get_user("delete-me@example.com"))

        async with self.session_factory() as session:
            result = await session.execute(
                select(func.count(LoginEvent.id)).where(
                    LoginEvent.user_id == user.id
                )
            )
            self.assertEqual(int(result.scalar_one()), 0)

            result = await session.execute(
                select(func.count(OTPChallenge.id)).where(
                    OTPChallenge.user_id == user.id
                )
            )
            self.assertEqual(int(result.scalar_one()), 0)

        self_delete = await self.client.delete(
            f"/admin/users/{admin.id}",
            headers=admin_headers,
        )
        self.assertEqual(self_delete.status_code, 400, self_delete.text)

        missing = await self.client.delete(
            "/admin/users/999999",
            headers=admin_headers,
        )
        self.assertEqual(missing.status_code, 404)

    async def test_login_history_tracks_only_successful_logins(self):
        admin_headers = await self.admin_headers()
        user = await self.create_verified_user("history@example.com")
        self.assertIsNotNone(user)

        history = await self.client.get(
            f"/admin/users/{user.id}/logins",
            headers=admin_headers,
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 0)

        failed = await self.login(user.email, "WrongPassword1!")
        self.assertEqual(failed.status_code, 401)

        history = await self.client.get(
            f"/admin/users/{user.id}/logins",
            headers=admin_headers,
        )
        self.assertEqual(history.json()["total"], 0)

        first = await self.login(user.email)
        self.assertEqual(first.status_code, 200)
        second = await self.login(user.email)
        self.assertEqual(second.status_code, 200)

        history = await self.client.get(
            f"/admin/users/{user.id}/logins",
            headers=admin_headers,
        )
        self.assertEqual(history.status_code, 200, history.text)
        body = history.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["last_login_method"], "password")
        self.assertEqual(
            [item["method"] for item in body["items"]],
            ["password", "password"],
        )

        google_payload = {
            "email": "google-member@example.com",
            "sub": "google-subject-admin-test",
            "name": "Google Member",
            "email_verified": True,
        }
        original_verify = main.id_token.verify_oauth2_token
        main.id_token.verify_oauth2_token = (
            lambda token, request, audience: google_payload
        )

        try:
            google_login = await self.client.post(
                "/auth/google",
                json={"id_token": "fake-google-token"},
            )
        finally:
            main.id_token.verify_oauth2_token = original_verify

        self.assertEqual(google_login.status_code, 200, google_login.text)

        google_user = await self.get_user("google-member@example.com")
        self.assertIsNotNone(google_user)
        google_history = await self.client.get(
            f"/admin/users/{google_user.id}/logins",
            headers=admin_headers,
        )
        self.assertEqual(google_history.status_code, 200, google_history.text)
        self.assertEqual(google_history.json()["total"], 1)
        self.assertEqual(
            google_history.json()["items"][0]["method"],
            "google",
        )
        self.assertEqual(google_history.json()["last_login_method"], "google")

        blocked_google = await self.client.post(
            f"/admin/users/{google_user.id}/block",
            headers=admin_headers,
        )
        self.assertEqual(blocked_google.status_code, 200, blocked_google.text)

        original_verify = main.id_token.verify_oauth2_token
        main.id_token.verify_oauth2_token = (
            lambda token, request, audience: google_payload
        )

        try:
            blocked_google_login = await self.client.post(
                "/auth/google",
                json={"id_token": "fake-google-token"},
            )
        finally:
            main.id_token.verify_oauth2_token = original_verify

        self.assertEqual(blocked_google_login.status_code, 403)
        self.assertNotIn("access_token", blocked_google_login.json())


if __name__ == "__main__":
    unittest.main()

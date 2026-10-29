import hashlib
import hmac
import os
import secrets

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

import bcrypt
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from database import Base, engine, get_db
from email_service import (
    EmailSendError,
    send_otp_email,
    send_password_reset_email,
)
from models import (
    LoginEvent,
    OTPChallenge,
    PasswordResetChallenge,
    User,
)
from rate_limit import rate_limiter

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env")


def _read_int_env(name: str, default: int) -> int | None:
    raw_value = os.getenv(name)

    if raw_value is None or not raw_value.strip():
        return default

    try:
        return int(raw_value.strip())
    except ValueError:
        return None


JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "30"))

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

OTP_HMAC_SECRET = os.getenv("OTP_HMAC_SECRET", "").strip()
OTP_EXPIRY_MINUTES = 10
RESET_TOKEN_EXPIRY_MINUTES = 10

OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = int(
    os.getenv("OTP_RESEND_COOLDOWN_SECONDS", "30")
)

RATE_LIMIT_SIGNUP = (10, 60)
RATE_LIMIT_LOGIN = (10, 60)
RATE_LIMIT_VERIFY = (20, 60)
RATE_LIMIT_RESEND = (10, 60)
RATE_LIMIT_FORGOT = (5, 60)
RATE_LIMIT_RESET = (10, 60)

ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.getenv("ADMIN_EMAILS", "").split(",")
    if email.strip()
}

if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY is missing from .env")

app = FastAPI(title="Prices Indicator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
        "http://127.0.0.1:4200",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bearer_scheme = HTTPBearer(auto_error=False)


@app.on_event("startup")
async def startup():
    # create_all() creates missing tables but does not alter existing ones.
    # These idempotent statements add the admin user-management columns to
    # databases created by earlier versions without dropping any data.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "is_blocked BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "last_login_at TIMESTAMP WITH TIME ZONE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "last_login_method VARCHAR(20)"
            )
        )

    if not OTP_HMAC_SECRET:
        print(
            "Warning: OTP_HMAC_SECRET is missing. Signup verification "
            "codes cannot be hashed, so signup will be refused until "
            "OTP_HMAC_SECRET is set."
        )


# ----------------------------
# Request schemas
# ----------------------------

class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class VerifyOTPRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    otp: str = Field(pattern=r"^\d{6}$")


class ResendOTPRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResetPasswordRequest(BaseModel):
    reset_token: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class GoogleLoginRequest(BaseModel):
    id_token: str = Field(min_length=1)


class AdminUserUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: EmailStr | None = None
    role: Literal["user", "admin"] | None = None


# ----------------------------
# Helpers
# ----------------------------

def normalize_email(email: str) -> str:
    return email.strip().lower()


def role_for_email(email: str) -> str:
    return (
        "admin"
        if normalize_email(email) in ADMIN_EMAILS
        else "user"
    )


def hash_password(password: str) -> str:
    # bcrypt only accepts up to 72 bytes.
    password_bytes = password.encode("utf-8")

    if len(password_bytes) > 72:
        raise HTTPException(
            status_code=400,
            detail="Password must be no longer than 72 UTF-8 bytes.",
        )

    return bcrypt.hashpw(
        password_bytes,
        bcrypt.gensalt(),
    ).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }

    return jwt.encode(
        payload,
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )


def user_response(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "email_verified": user.email_verified,
    }


def account_status(user: User) -> str:
    if user.is_blocked:
        return "blocked"

    if not user.email_verified:
        return "inactive"

    return "active"


def authentication_provider(user: User) -> str:
    providers = []

    if user.password_hash:
        providers.append("password")

    if user.google_sub:
        providers.append("google")

    return "+".join(providers) if providers else "unknown"


def admin_user_response(user: User) -> dict:
    # Only expose fields the admin dashboard needs. Password hashes,
    # Google subject identifiers, OTP data, and tokens stay server-side.
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "email_verified": user.email_verified,
        "status": account_status(user),
        "is_blocked": user.is_blocked,
        "auth_provider": authentication_provider(user),
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
        "last_login_method": user.last_login_method,
    }


def login_event_response(event: LoginEvent) -> dict:
    return {
        "id": event.id,
        "method": event.method,
        "logged_in_at": event.logged_in_at,
    }


async def record_successful_login(
    user: User,
    method: str,
    db: AsyncSession,
) -> None:
    now = datetime.now(timezone.utc)
    user.last_login_at = now
    user.last_login_method = method

    if user.id is None:
        await db.flush()

    db.add(
        LoginEvent(
            user_id=user.id,
            method=method,
            logged_in_at=now,
        )
    )


async def active_admin_count(
    db: AsyncSession,
    exclude_user_id: int | None = None,
) -> int:
    conditions = [
        User.role == "admin",
        User.is_blocked.is_(False),
        User.email_verified.is_(True),
    ]

    if exclude_user_id is not None:
        conditions.append(User.id != exclude_user_id)

    result = await db.execute(
        select(func.count(User.id)).where(*conditions)
    )

    return int(result.scalar_one())


async def would_remove_last_active_admin(
    user: User,
    db: AsyncSession,
) -> bool:
    is_active_admin = (
        user.role == "admin"
        and not user.is_blocked
        and user.email_verified
    )

    if not is_active_admin:
        return False

    return await active_admin_count(db, exclude_user_id=user.id) == 0


async def get_user_or_404(
    db: AsyncSession,
    user_id: int,
) -> User:
    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    return user


def promote_allowlisted_admin(user: User) -> None:
    # ADMIN_EMAILS remains a trusted bootstrap source. It can promote an
    # account, but never silently demotes an admin managed from the panel.
    if role_for_email(user.email) == "admin":
        user.role = "admin"


def otp_digest(
    challenge_id: str,
    otp: str,
    unavailable_detail: str = "Signup is temporarily unavailable.",
) -> str:
    if not OTP_HMAC_SECRET:
        raise HTTPException(
            status_code=503,
            detail=unavailable_detail,
        )

    return hmac.new(
        OTP_HMAC_SECRET.encode("utf-8"),
        f"{challenge_id}:{otp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def reset_token_digest(token: str) -> str:
    if not OTP_HMAC_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Password recovery is temporarily unavailable.",
        )

    return hmac.new(
        OTP_HMAC_SECRET.encode("utf-8"),
        f"password-reset:{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


def enforce_rate_limit(
    scope: str,
    request: Request,
    email: str,
    limits: tuple[int, int],
    include_ip: bool = True,
) -> None:
    limit, window = limits
    entries = []

    if include_ip:
        entries.append(("ip", client_ip(request)))

    if email:
        entries.append(("email", email))

    for kind, value in entries:
        retry_after = rate_limiter.hit(
            scope,
            kind,
            value,
            limit,
            window,
        )

        if retry_after:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )



async def issue_otp(user: User, db: AsyncSession) -> str:
    otp = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = secrets.token_urlsafe(32)

    # Keep older challenges usable until the new email is accepted. If the
    # provider fails, the user can still use their previous code or retry.
    existing = await db.execute(
        select(OTPChallenge).where(
            OTPChallenge.user_id == user.id,
            OTPChallenge.consumed.is_(False),
        )
    )
    old_challenges = list(existing.scalars().all())

    challenge = OTPChallenge(
        challenge_id=challenge_id,
        user_id=user.id,
        otp_hash=otp_digest(challenge_id, otp),
        expires_at=(
            datetime.now(timezone.utc)
            + timedelta(minutes=OTP_EXPIRY_MINUTES)
        ),
        attempts=0,
        consumed=False,
    )

    db.add(challenge)
    await db.commit()

    try:
        await send_otp_email(user.email, otp, user.name)
    except Exception as exc:
        challenge.consumed = True
        await db.commit()

        category = (
            exc.category
            if isinstance(exc, EmailSendError)
            else "unknown"
        )
        print(f"OTP email delivery failed: {category}")

        raise HTTPException(
            status_code=503,
            detail="Could not send verification email, try again",
        ) from exc

    for old_challenge in old_challenges:
        old_challenge.consumed = True

    if old_challenges:
        await db.commit()

    return challenge_id


async def issue_password_reset_otp(
    user: User,
    db: AsyncSession,
) -> str:
    otp = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = secrets.token_urlsafe(32)

    existing = await db.execute(
        select(PasswordResetChallenge).where(
            PasswordResetChallenge.user_id == user.id,
            PasswordResetChallenge.consumed.is_(False),
        )
    )
    old_challenges = list(existing.scalars().all())

    challenge = PasswordResetChallenge(
        challenge_id=challenge_id,
        user_id=user.id,
        otp_hash=otp_digest(
            challenge_id,
            otp,
            "Password recovery is temporarily unavailable.",
        ),
        expires_at=(
            datetime.now(timezone.utc)
            + timedelta(minutes=OTP_EXPIRY_MINUTES)
        ),
        attempts=0,
        otp_verified=False,
        consumed=False,
    )

    db.add(challenge)
    await db.commit()

    try:
        await send_password_reset_email(user.email, otp, user.name)
    except Exception as exc:
        challenge.consumed = True
        await db.commit()

        category = (
            exc.category
            if isinstance(exc, EmailSendError)
            else "unknown"
        )
        print(f"Password reset email delivery failed: {category}")

        raise HTTPException(
            status_code=503,
            detail="Could not send recovery email, try again",
        ) from exc

    for old_challenge in old_challenges:
        old_challenge.consumed = True
        old_challenge.reset_token_hash = None
        old_challenge.reset_token_expires_at = None

    if old_challenges:
        await db.commit()

    return challenge_id


# ----------------------------
# JWT authentication
# ----------------------------

async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
        )
        subject = payload.get("sub")

        if not subject:
            raise JWTError("Missing subject")

        user_id = int(subject)

    except (JWTError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
        )

    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found.",
        )

    # Stateless JWTs are checked against the current account row on every
    # request, so blocking or deleting an account invalidates existing tokens.
    if user.is_blocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been blocked.",
        )

    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification is required.",
        )

    # The server's allowlist can promote an account, but role edits made in
    # the admin panel are persisted and are not overwritten on every request.
    if role_for_email(user.email) == "admin" and user.role != "admin":
        user.role = "admin"
        await db.commit()
        await db.refresh(user)

    return user


async def get_admin_user(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required.",
        )

    return user


# ----------------------------
# Signup
# ----------------------------

@app.post("/auth/signup", status_code=201)
async def signup(
    data: SignupRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    email = normalize_email(data.email)
    name = data.name.strip()

    enforce_rate_limit("signup", request, email, RATE_LIMIT_SIGNUP)

    if not OTP_HMAC_SECRET:
        print("Signup refused: OTP_HMAC_SECRET is not configured.")
        raise HTTPException(
            status_code=503,
            detail="Signup is temporarily unavailable.",
        )

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name is required.",
        )

    result = await db.execute(
        select(User).where(User.email == email)
    )
    existing_user = result.scalar_one_or_none()

    if existing_user and (
        existing_user.email_verified or existing_user.is_blocked
    ):
        # Generic response so an attacker cannot probe which emails exist,
        # and blocked accounts cannot restart verification.
        return JSONResponse(
            status_code=200,
            content={
                "message": (
                    "If this email is available, a verification code has "
                    "been sent."
                )
            },
        )

    if existing_user:
        # Reuse the unverified account and refresh its credentials.
        user = existing_user
        user.name = name
        user.password_hash = hash_password(data.password)
        promote_allowlisted_admin(user)
        await db.commit()
        await db.refresh(user)
    else:
        user = User(
            name=name,
            email=email,
            password_hash=hash_password(data.password),
            email_verified=False,
            role=role_for_email(email),
        )
        db.add(user)

        try:
            await db.commit()
        except IntegrityError:
            # Handles the small race window between the lookup and insert.
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="An account with this email already exists.",
            )

        await db.refresh(user)

    # The account stays unverified if delivery fails; issue_otp raises a
    # single generic 503 and never exposes a challenge id.
    challenge_id = await issue_otp(user, db)

    return {
        "message": "Verification code sent to your email.",
        "challenge_id": challenge_id,
        "resend_after_seconds": OTP_RESEND_COOLDOWN_SECONDS,
    }


# ----------------------------
# Password login
# ----------------------------

@app.post("/auth/login")
async def login(
    data: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    email = normalize_email(data.email)

    enforce_rate_limit("login", request, email, RATE_LIMIT_LOGIN)

    result = await db.execute(
        select(User).where(User.email == email)
    )
    user = result.scalar_one_or_none()

    # Password verification happens before any account-status response.
    if (
        user is None
        or user.password_hash is None
        or not verify_password(data.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    if user.is_blocked:
        raise HTTPException(
            status_code=403,
            detail="This account has been blocked.",
        )

    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Unable to sign in. Please verify your email.",
        )

    # Password login is the only factor after email verification.
    promote_allowlisted_admin(user)
    await record_successful_login(user, "password", db)
    await db.commit()
    await db.refresh(user)

    return {
        "message": "Login successful.",
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": user_response(user),
    }


# ----------------------------
# Resend verification OTP
# ----------------------------

@app.post("/auth/resend-otp")
async def resend_otp(
    data: ResendOTPRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(OTPChallenge)
        .where(OTPChallenge.challenge_id == data.challenge_id)
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()

    if challenge is None or challenge.consumed:
        raise HTTPException(
            status_code=400,
            detail=(
                "This verification session is no longer valid. "
                "Please start signup again."
            ),
        )

    result = await db.execute(
        select(User).where(User.id == challenge.user_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "This verification session is no longer valid. "
                "Please start signup again."
            ),
        )

    if user.is_blocked:
        raise HTTPException(
            status_code=403,
            detail="This account has been blocked.",
        )

    enforce_rate_limit("resend", request, user.email, RATE_LIMIT_RESEND)

    if user.email_verified:
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail="This email is already verified. You can log in.",
        )

    # Throttle repeated resend requests. The referenced challenge is locked
    # above, and issue_otp invalidates every older unused challenge.
    latest_result = await db.execute(
        select(OTPChallenge)
        .where(OTPChallenge.user_id == user.id)
        .order_by(
            OTPChallenge.created_at.desc(),
            OTPChallenge.id.desc(),
        )
        .limit(1)
    )
    latest_challenge = latest_result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if latest_challenge is not None:
        created_at = latest_challenge.created_at

        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        elapsed = (now - created_at).total_seconds()

        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            retry_after = max(
                1,
                int(OTP_RESEND_COOLDOWN_SECONDS - elapsed),
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    "Please wait before requesting another verification "
                    f"code. Try again in {retry_after} seconds."
                ),
                headers={"Retry-After": str(retry_after)},
            )

    challenge_id = await issue_otp(user, db)

    return {
        "message": "A new verification code has been sent to your email.",
        "challenge_id": challenge_id,
        "resend_after_seconds": OTP_RESEND_COOLDOWN_SECONDS,
    }


# ----------------------------
# Verify email OTP
# ----------------------------

@app.post("/auth/verify-otp")
async def verify_otp(
    data: VerifyOTPRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    enforce_rate_limit(
        "verify",
        request,
        "",
        RATE_LIMIT_VERIFY,
        include_ip=True,
    )

    result = await db.execute(
        select(OTPChallenge)
        .where(OTPChallenge.challenge_id == data.challenge_id)
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()

    if challenge is None or challenge.consumed:
        raise HTTPException(
            status_code=400,
            detail="This verification request is invalid or has already been used.",
        )

    now = datetime.now(timezone.utc)
    expires_at = challenge.expires_at

    # Some PostgreSQL configurations/drivers can return naive datetimes.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if now >= expires_at:
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail="Verification code expired. Request a new code.",
        )

    if challenge.attempts >= OTP_MAX_ATTEMPTS:
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many incorrect attempts. Request a new code.",
        )

    submitted_hash = otp_digest(data.challenge_id, data.otp)

    if not hmac.compare_digest(
        challenge.otp_hash,
        submitted_hash,
    ):
        challenge.attempts += 1

        if challenge.attempts >= OTP_MAX_ATTEMPTS:
            challenge.consumed = True
            await db.commit()
            raise HTTPException(
                status_code=429,
                detail="Too many incorrect attempts. Request a new code.",
            )

        await db.commit()

        raise HTTPException(
            status_code=400,
            detail="Incorrect verification code.",
        )

    result = await db.execute(
        select(User).where(User.id == challenge.user_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=404,
            detail="User account not found.",
        )

    if user.is_blocked:
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=403,
            detail="This account has been blocked.",
        )

    challenge.consumed = True
    user.email_verified = True
    promote_allowlisted_admin(user)

    await db.commit()
    await db.refresh(user)

    return {
        "message": "Email verified successfully. You can now log in.",
        "user": user_response(user),
    }


# ----------------------------
# Forgot password
# ----------------------------

PASSWORD_RESET_GENERIC_MESSAGE = (
    "If an account exists for that email, a password reset code has "
    "been sent."
)


@app.post("/auth/forgot-password")
async def forgot_password(
    data: ForgotPasswordRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    email = normalize_email(data.email)

    enforce_rate_limit("forgot", request, email, RATE_LIMIT_FORGOT)

    response = {
        "message": PASSWORD_RESET_GENERIC_MESSAGE,
        "challenge_id": secrets.token_urlsafe(32),
        "resend_after_seconds": OTP_RESEND_COOLDOWN_SECONDS,
    }

    if not OTP_HMAC_SECRET:
        return response

    result = await db.execute(
        select(User).where(User.email == email)
    )
    user = result.scalar_one_or_none()

    # Unknown, blocked, and Google-only accounts receive the same response.
    # A Google-only account has no local password to reset.
    if (
        user is None
        or user.is_blocked
        or user.password_hash is None
    ):
        return response

    try:
        challenge_id = await issue_password_reset_otp(user, db)
    except HTTPException as exc:
        if exc.status_code == 503:
            return response
        raise

    response["challenge_id"] = challenge_id
    return response


@app.post("/auth/resend-password-reset")
async def resend_password_reset(
    data: ResendOTPRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(PasswordResetChallenge)
        .where(
            PasswordResetChallenge.challenge_id
            == data.challenge_id
        )
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()

    if (
        challenge is None
        or challenge.consumed
        or challenge.otp_verified
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "This password reset session is no longer valid. "
                "Please start again."
            ),
        )

    result = await db.execute(
        select(User).where(User.id == challenge.user_id)
    )
    user = result.scalar_one_or_none()

    if (
        user is None
        or user.is_blocked
        or user.password_hash is None
    ):
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                "This password reset session is no longer valid. "
                "Please start again."
            ),
        )

    enforce_rate_limit(
        "reset_resend",
        request,
        user.email,
        RATE_LIMIT_RESEND,
    )

    latest_result = await db.execute(
        select(PasswordResetChallenge)
        .where(PasswordResetChallenge.user_id == user.id)
        .order_by(
            PasswordResetChallenge.created_at.desc(),
            PasswordResetChallenge.id.desc(),
        )
        .limit(1)
    )
    latest_challenge = latest_result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if latest_challenge is not None:
        created_at = as_utc(latest_challenge.created_at)
        elapsed = (now - created_at).total_seconds()

        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            retry_after = max(
                1,
                int(OTP_RESEND_COOLDOWN_SECONDS - elapsed),
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    "Please wait before requesting another reset code. "
                    f"Try again in {retry_after} seconds."
                ),
                headers={"Retry-After": str(retry_after)},
            )

    challenge_id = await issue_password_reset_otp(user, db)

    return {
        "message": "A new password reset code has been sent to your email.",
        "challenge_id": challenge_id,
        "resend_after_seconds": OTP_RESEND_COOLDOWN_SECONDS,
    }


@app.post("/auth/verify-password-reset-otp")
async def verify_password_reset_otp(
    data: VerifyOTPRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    enforce_rate_limit(
        "reset_verify",
        request,
        "",
        RATE_LIMIT_VERIFY,
        include_ip=True,
    )

    result = await db.execute(
        select(PasswordResetChallenge)
        .where(
            PasswordResetChallenge.challenge_id
            == data.challenge_id
        )
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()

    if (
        challenge is None
        or challenge.consumed
        or challenge.otp_verified
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "This password reset request is invalid or has "
                "already been used."
            ),
        )

    now = datetime.now(timezone.utc)
    expires_at = as_utc(challenge.expires_at)

    if now >= expires_at:
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                "Password reset code expired. Request a new code."
            ),
        )

    if challenge.attempts >= OTP_MAX_ATTEMPTS:
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many incorrect attempts. Request a new code.",
        )

    submitted_hash = otp_digest(
        data.challenge_id,
        data.otp,
        "Password recovery is temporarily unavailable.",
    )

    if not hmac.compare_digest(
        challenge.otp_hash,
        submitted_hash,
    ):
        challenge.attempts += 1

        if challenge.attempts >= OTP_MAX_ATTEMPTS:
            challenge.consumed = True
            await db.commit()
            raise HTTPException(
                status_code=429,
                detail=(
                    "Too many incorrect attempts. "
                    "Request a new code."
                ),
            )

        await db.commit()

        raise HTTPException(
            status_code=400,
            detail="Incorrect verification code.",
        )

    result = await db.execute(
        select(User).where(User.id == challenge.user_id)
    )
    user = result.scalar_one_or_none()

    if (
        user is None
        or user.is_blocked
        or user.password_hash is None
    ):
        challenge.consumed = True
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                "This password reset request is invalid or has "
                "expired."
            ),
        )

    reset_token = secrets.token_urlsafe(48)
    challenge.otp_verified = True
    challenge.reset_token_hash = reset_token_digest(reset_token)
    challenge.reset_token_expires_at = (
        now + timedelta(minutes=RESET_TOKEN_EXPIRY_MINUTES)
    )

    await db.commit()

    return {
        "message": "Code verified. You can now set a new password.",
        "reset_token": reset_token,
        "expires_in_seconds": RESET_TOKEN_EXPIRY_MINUTES * 60,
    }


@app.post("/auth/reset-password")
async def reset_password(
    data: ResetPasswordRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    enforce_rate_limit(
        "reset_password",
        request,
        "",
        RATE_LIMIT_RESET,
        include_ip=True,
    )

    token_hash = reset_token_digest(data.reset_token)

    result = await db.execute(
        select(PasswordResetChallenge)
        .where(
            PasswordResetChallenge.reset_token_hash == token_hash
        )
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    token_expires_at = (
        as_utc(challenge.reset_token_expires_at)
        if challenge is not None
        and challenge.reset_token_expires_at is not None
        else None
    )

    if (
        challenge is None
        or challenge.consumed
        or not challenge.otp_verified
        or token_expires_at is None
        or now >= token_expires_at
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "This password reset link is invalid or has expired."
            ),
        )

    result = await db.execute(
        select(User).where(User.id == challenge.user_id)
    )
    user = result.scalar_one_or_none()

    if (
        user is None
        or user.is_blocked
        or user.password_hash is None
    ):
        challenge.consumed = True
        challenge.reset_token_hash = None
        challenge.reset_token_expires_at = None
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                "This password reset link is invalid or has expired."
            ),
        )

    new_password_hash = hash_password(data.password)

    user.password_hash = new_password_hash

    # Consume the authorizing challenge and every other outstanding reset
    # challenge for this user so neither the OTP nor any reset token can be
    # reused.
    await db.execute(
        update(PasswordResetChallenge)
        .where(
            PasswordResetChallenge.user_id == user.id,
            PasswordResetChallenge.consumed.is_(False),
        )
        .values(
            consumed=True,
            reset_token_hash=None,
            reset_token_expires_at=None,
        )
    )
    await db.commit()

    return {
        "message": (
            "Password reset successfully. You can now log in."
        )
    }


# ----------------------------
# Google Sign-In
# ----------------------------

@app.post("/auth/google")
async def google_login(
    data: GoogleLoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="Google Sign-In is not configured on the server.",
        )

    try:
        google_info = await run_in_threadpool(
            id_token.verify_oauth2_token,
            data.id_token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except Exception as exc:
        import traceback

        print(f"Google ID token verification failed: {type(exc).__name__}")
        print(f"Error details: {exc}")
        traceback.print_exc()

        raise HTTPException(
            status_code=401,
            detail="Google token is invalid or expired. Please try again.",
        ) from exc

    email = normalize_email(google_info.get("email", ""))
    google_sub = google_info.get("sub")
    name = (google_info.get("name") or email.split("@")[0]).strip()
    email_verified = google_info.get("email_verified") is True

    if not email or not google_sub or not email_verified:
        raise HTTPException(
            status_code=401,
            detail="Google must provide a verified email address.",
        )

    # Prefer an existing Google-linked account.
    result = await db.execute(
        select(User).where(User.google_sub == google_sub)
    )
    user = result.scalar_one_or_none()

    if user is None:
        # Link Google to an existing password account by email.
        result = await db.execute(
            select(User).where(User.email == email)
        )
        user = result.scalar_one_or_none()

    if user is not None and user.is_blocked:
        raise HTTPException(
            status_code=403,
            detail="This account has been blocked.",
        )

    if user is None:
        user = User(
            name=name[:100] or "User",
            email=email,
            google_sub=google_sub,
            email_verified=True,
            role=role_for_email(email),
        )
        db.add(user)

    else:
        # Do not overwrite a different Google identity already linked.
        if user.google_sub and user.google_sub != google_sub:
            raise HTTPException(
                status_code=409,
                detail="A different Google account is already linked to this email.",
            )

        user.google_sub = google_sub
        user.email_verified = True
        promote_allowlisted_admin(user)

        if not user.name and name:
            user.name = name[:100]

    await record_successful_login(user, "google", db)
    await db.commit()
    await db.refresh(user)

    return {
        "message": "Google sign-in successful.",
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": user_response(user),
    }


# ----------------------------
# Current user
# ----------------------------

@app.get("/auth/me")
async def get_me(
    user: Annotated[User, Depends(get_current_user)],
):
    return user_response(user)


# ----------------------------
# Protected admin example
# ----------------------------

@app.get("/admin/check")
async def admin_check(
    admin: Annotated[User, Depends(get_admin_user)],
):
    return {
        "message": "Admin access verified.",
        "user": user_response(admin),
    }


# ----------------------------
# Admin user management
# ----------------------------

@app.get("/admin/users")
async def list_admin_users(
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    search: Annotated[str | None, Query(max_length=100)] = None,
    status_filter: Annotated[
        Literal["all", "active", "blocked", "inactive"],
        Query(alias="status"),
    ] = "all",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 10,
):
    filters = []
    search_term = (search or "").strip()

    if search_term:
        pattern = f"%{search_term}%"
        conditions = [
            User.name.ilike(pattern),
            User.email.ilike(pattern),
        ]

        if search_term.isdigit():
            conditions.append(User.id == int(search_term))

        filters.append(or_(*conditions))

    if status_filter == "active":
        filters.extend(
            [
                User.is_blocked.is_(False),
                User.email_verified.is_(True),
            ]
        )
    elif status_filter == "blocked":
        filters.append(User.is_blocked.is_(True))
    elif status_filter == "inactive":
        filters.extend(
            [
                User.is_blocked.is_(False),
                User.email_verified.is_(False),
            ]
        )

    base_query = select(User)

    if filters:
        base_query = base_query.where(*filters)

    total_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = int(total_result.scalar_one())

    result = await db.execute(
        base_query
        .order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    users = list(result.scalars().all())
    pages = max(1, (total + page_size - 1) // page_size)

    return {
        "items": [admin_user_response(user) for user in users],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@app.get("/admin/users/{user_id}")
async def get_admin_user_detail(
    user_id: int,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user = await get_user_or_404(db, user_id)
    return admin_user_response(user)


@app.get("/admin/users/{user_id}/logins")
async def list_user_logins(
    user_id: int,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    user = await get_user_or_404(db, user_id)

    total_result = await db.execute(
        select(func.count(LoginEvent.id)).where(
            LoginEvent.user_id == user.id
        )
    )
    total = int(total_result.scalar_one())

    result = await db.execute(
        select(LoginEvent)
        .where(LoginEvent.user_id == user.id)
        .order_by(
            LoginEvent.logged_in_at.desc(),
            LoginEvent.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    events = list(result.scalars().all())
    pages = max(1, (total + page_size - 1) // page_size)

    return {
        "items": [login_event_response(event) for event in events],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "last_login_at": user.last_login_at,
        "last_login_method": user.last_login_method,
    }


@app.patch("/admin/users/{user_id}")
async def update_admin_user(
    user_id: int,
    data: AdminUserUpdateRequest,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user = await get_user_or_404(db, user_id)
    fields_set = data.model_fields_set

    if not fields_set:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No editable fields were provided.",
        )

    if "name" in fields_set and data.name is not None:
        name = data.name.strip()

        if not name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Name is required.",
            )

        user.name = name

    if "email" in fields_set and data.email is not None:
        email = normalize_email(str(data.email))

        if len(email) > 320:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Email address is too long.",
            )

        if email != user.email:
            if user.id == admin.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "You cannot change your own email address from "
                        "this panel."
                    ),
                )

            duplicate_result = await db.execute(
                select(User.id).where(
                    User.email == email,
                    User.id != user.id,
                )
            )

            if duplicate_result.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An account with this email already exists.",
                )

            user.email = email
            user.email_verified = False
            user.google_sub = None
            promote_allowlisted_admin(user)

    if "role" in fields_set and data.role is not None:
        if data.role != user.role:
            if user.id == admin.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="You cannot change your own role.",
                )

            if data.role == "user":
                if role_for_email(user.email) == "admin":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "This account is an administrator through "
                            "the server allowlist and cannot be demoted "
                            "here."
                        ),
                    )

                if await would_remove_last_active_admin(user, db):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The last active administrator cannot be demoted.",
                    )

            user.role = data.role

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    await db.refresh(user)
    return admin_user_response(user)


@app.post("/admin/users/{user_id}/block")
async def block_admin_user(
    user_id: int,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user = await get_user_or_404(db, user_id)

    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot block your own account.",
        )

    if not user.is_blocked:
        if await would_remove_last_active_admin(user, db):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The last active administrator cannot be blocked.",
            )

        user.is_blocked = True
        await db.commit()
        await db.refresh(user)

    return admin_user_response(user)


@app.post("/admin/users/{user_id}/unblock")
async def unblock_admin_user(
    user_id: int,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user = await get_user_or_404(db, user_id)

    if user.is_blocked:
        user.is_blocked = False
        await db.commit()
        await db.refresh(user)

    return admin_user_response(user)


@app.delete("/admin/users/{user_id}")
async def delete_admin_user(
    user_id: int,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user = await get_user_or_404(db, user_id)

    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )

    if await would_remove_last_active_admin(user, db):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The last active administrator cannot be deleted.",
        )

    # Delete dependent records explicitly, then the user row. No unrelated
    # tables are touched.
    await db.execute(
        delete(LoginEvent).where(LoginEvent.user_id == user.id)
    )
    await db.execute(
        delete(OTPChallenge).where(OTPChallenge.user_id == user.id)
    )
    await db.execute(delete(User).where(User.id == user.id))
    await db.commit()

    return {
        "message": "User deleted successfully.",
        "user_id": user_id,
    }


@app.get("/")
async def root():
    return {"message": "Prices Indicator API is running."}
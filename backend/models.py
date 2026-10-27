from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(320),
        unique=True,
        index=True,
        nullable=False,
    )

    password_hash: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    google_sub: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
    )

    email_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(20),
        default="user",
        nullable=False,
    )

    is_blocked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_login_method: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    otp_challenges: Mapped[list["OTPChallenge"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class OTPChallenge(Base):
    __tablename__ = "otp_challenges"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    challenge_id: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    otp_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    consumed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    user: Mapped["User"] = relationship(
        back_populates="otp_challenges",
    )


class LoginEvent(Base):
    __tablename__ = "login_events"

    __table_args__ = (
        Index(
            "ix_login_events_user_logged_in_at",
            "user_id",
            "logged_in_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    method: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    logged_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
# Prices Indicator — Complete Project Guide

A consolidated, detailed reference for the Prices Indicator repository.

This document combines the foundational analysis and Chapters 1–21 into a single guide. It explains what the project does today, how each subsystem works, where the code lives, what is implemented versus planned, how requests travel through the system, how to debug failures, and how to evolve the database safely.

> **Scope and status:** This guide is based on read-only analysis of the repository. It does not claim that the project is production-ready. Verification results are recorded as observed at analysis time and were not re-run while creating this document.

---

## Verification Snapshot

| Item | Result |
|---|---|
| Python | 3.11.9 |
| Node.js | 22.15.1 |
| npm | 10.9.2 |
| Backend tests | 77 passed: 57 auth, 8 admin, 12 password reset |
| Frontend type-check | `npx tsc --noEmit -p tsconfig.app.json` passed |
| Frontend production build | Passed with a bundle-size warning |
| Bundle warning | 520.19 kB exceeds the 500 kB budget |
| Frontend unit-test result | Final Karma result not confirmed |
| Live PostgreSQL schema | Not inspected; schema facts come from ORM models and startup DDL |
| Scraper live execution | Not verified |
| Git HEAD at analysis time | `9c9d54a removed extra files` |
| Uncommitted change | `frontend/src/auth/auth.html` outer auth container styling |

The backend test suite uses temporary PostgreSQL schemas, so running it mutates a live database temporarily. Do not run it when a task forbids database mutation.

---

## Table of Contents

### Part I — Foundations

- A. System Overview and Mental Model
- B. Repository Map
- C. Technology Stack
- D. Feature Inventory and Status
- E. Dependency and Communication Map
- F. Architecture Reading Roadmap

### Part II — Detailed Chapters

- Chapter 1 — Project Status and Purpose
- Chapter 2 — Running the Project
- Chapter 3 — Configuration and Secrets
- Chapter 4 — FastAPI Lifecycle and Request Pipeline
- Chapter 5 — Database Schema and Models
- Chapter 6 — Authentication Foundations
- Chapter 7 — Signup and Email OTP
- Chapter 8 — Login, Blocking, and Login History
- Chapter 9 — Google Sign-In
- Chapter 10 — Password Reset
- Chapter 11 — Admin User Management
- Chapter 12 — User Dashboard, Routing, and Session Handling
- Chapter 13 — The Scraper Subsystem
- Chapter 14 — Product-Comparison Integration Design
- Chapter 15 — Testing
- Chapter 16 — Deployment and Operations
- Chapter 17 — Security Review
- Chapter 18 — Final Assessment and Roadmap
- Chapter 19 — End-to-End Walkthroughs
- Chapter 20 — Debugging Playbook
- Chapter 21 — Database Migrations and Safe Schema Evolution

### Appendices

- Appendix A — Endpoint Reference
- Appendix B — Environment Variables
- Appendix C — Data Model Reference
- Appendix D — Commands
- Appendix E — Glossary
- Appendix F — Known Limitations and Verification Boundaries

---

# Part I — Foundations

## A. System Overview and Mental Model

Prices Indicator is a price-comparison project with two largely separate halves:

1. **A working authentication and account-management application.**
2. **A collection of experimental retailer scrapers that are not connected to the application.**

The authentication half includes signup, email verification, login, Google Sign-In, password reset, role-based access, user blocking, login history, an admin dashboard, and a basic user dashboard.

The scraper half includes seven standalone FastAPI applications that use Playwright and BeautifulSoup to extract product data from retailer websites.

There is no product database, no price-history storage, no comparison API, and no product-comparison user interface.

### The most important architectural fact

The project is **not yet one integrated system**.

```text
Auth application
    Angular 20  <---- HTTP ---->  FastAPI main backend  <---->  PostgreSQL

Scraper applications
    HTTP caller <---- HTTP ---->  Standalone scraper  <---->  Retailer website
                                      |
                                      +----> JSON response
                                      +----> No main database
                                      +----> No authentication
                                      +----> No shared schema
```

Connecting these halves requires domain modelling and orchestration, not just one function call.

### Core mental models

Think of the system as four layers:

```text
Presentation
    Angular components, forms, routes, guards, dashboards

Application
    FastAPI routes, validation, authentication, authorization, rate limiting

Persistence
    SQLAlchemy models, PostgreSQL tables, transactions

External services
    Google Identity, email providers, retailer websites, Playwright browsers
```

For authentication, the mental model is:

> The JWT proves that a user authenticated. The database proves whether that user may still act.

That is why every protected request reloads the user from PostgreSQL and checks account state.

For scraping, the mental model is:

> A scraper is a separate HTTP service that drives a browser and returns JSON. It is not part of the application domain.

### Current maturity

| Area | Maturity |
|---|---|
| Authentication | Functional MVP |
| Admin user management | Functional MVP |
| Frontend auth flows | Functional MVP |
| Scrapers | Experimental |
| Product comparison | Designed conceptually, not implemented |
| Deployment | Local development only |
| Security hardening | Partial |
| Observability | Minimal |
| Database migrations | Absent |

---

## B. Repository Map

The repository is small at the top level:

```text
Prices Indicator/
├── backend/
│   ├── main.py
│   ├── models.py
│   ├── database.py
│   ├── rate_limit.py
│   ├── email_service.py
│   ├── authorize_gmail.py
│   ├── requirements.txt
│   ├── .env.example
│   ├── tests/
│   │   ├── test_auth_flow.py
│   │   ├── test_admin_user_management.py
│   │   └── test_password_reset.py
│   └── Webscrappers/
│       ├── alfatah.py
│       ├── daraz.py
│       ├── mega.py
│       ├── naheed.py
│       ├── priceoye.py
│       ├── shopon.py
│       └── telex.py
└── frontend/
    ├── package.json
    ├── angular.json
    ├── tsconfig*.json
    └── src/
        ├── main.ts
        ├── auth-serv.ts
        ├── app/
        │   ├── app.ts
        │   ├── app.html
        │   ├── app.config.ts
        │   └── app.routes.ts
        ├── auth/
        ├── otp/
        ├── forgotpassword/
        ├── userdashboard/
        ├── admindashboard/
        └── auth/
            └── auth.guard.ts
```

### Main entry points

| Entry point | Purpose |
|---|---|
| `backend/main.py` | Main FastAPI application, all auth/admin/reset routes |
| `backend/models.py` | SQLAlchemy ORM schema |
| `backend/database.py` | Async engine, session factory, `get_db()` |
| `frontend/src/main.ts` | Angular bootstrap |
| `frontend/src/app/app.routes.ts` | Page routes and guards |
| `frontend/src/auth-serv.ts` | Backend HTTP client and browser token storage |
| `backend/Webscrappers/*.py` | Standalone scraper services |

### Absent top-level infrastructure

No root README, Dockerfile, Compose file, CI workflow, reverse-proxy configuration, process manager, or migration setup was found.

---

## C. Technology Stack

### Backend

| Technology | Role |
|---|---|
| Python 3.11 | Runtime |
| FastAPI | HTTP framework |
| Uvicorn | ASGI server |
| SQLAlchemy async | ORM and database access |
| asyncpg | PostgreSQL async driver |
| PostgreSQL | Relational database |
| bcrypt | Password hashing |
| python-jose | JWT creation and verification |
| google-auth | Google ID-token verification |
| httpx | HTTP client for email APIs |
| python-dotenv | `.env` loading |
| email-validator | Email validation through Pydantic |

### Frontend

| Technology | Role |
|---|---|
| Angular 20 | SPA framework |
| TypeScript 5.8 | Application language |
| Tailwind CSS 4.3 | Utility-first styling |
| RxJS 7.8 | HTTP observables |
| Reactive Forms | Auth and admin forms |
| Jasmine/Karma | Unit-test framework |

### Scrapers

| Technology | Role |
|---|---|
| FastAPI | Each scraper exposes its own HTTP API |
| Playwright | Browser automation |
| BeautifulSoup | HTML parsing in some scrapers |
| lxml | Parser backend |
| httpx | Some direct API requests |

### Dependency caveats

- `backend/requirements.txt` is unpinned.
- Scraper dependencies are not listed in the main requirements file.
- Playwright browsers must be installed separately.
- The backend uses the deprecated `@app.on_event("startup")` API.
- Angular imports are mostly eager.
- The frontend API URL is hard-coded.

---

## D. Feature Inventory and Status

| Feature | Status | Notes |
|---|---|---|
| Password signup | Implemented | Creates user and OTP challenge |
| Email OTP verification | Implemented | HMAC-hashed OTP, expiry, attempt limit |
| OTP resend | Implemented | Cooldown and rate limiting |
| Password login | Implemented | bcrypt verification, JWT issuance |
| Blocked-user enforcement | Implemented | Checked on every protected request |
| Email-verification enforcement | Implemented | Unverified users cannot log in |
| Login history | Implemented | Successful logins only |
| Google Sign-In | Implemented | Server-side token verification |
| Password reset | Implemented | OTP then single-use reset token |
| Resend password-reset OTP | Implemented | Cooldown and challenge checks |
| Admin list/search/filter | Implemented | Pagination and status filters |
| Admin edit user | Implemented | Name, email, role API support |
| Admin block/unblock | Implemented | Self-block and last-admin protection |
| Admin delete user | Implemented | Cascades through relationships |
| Admin login history | Implemented | Method and timestamp only |
| User dashboard | Implemented | Account information only |
| Route guards | Implemented | Token presence and admin role |
| Token refresh | Missing | No refresh tokens |
| Server-side token revocation | Missing | Old JWTs survive reset/logout |
| MFA | Missing | No second factor for admins or users |
| Audit log | Missing | Admin actions not recorded |
| Product database | Missing | No product/offer/price-history tables |
| Product comparison API | Missing | No main-backend product routes |
| Product comparison UI | Missing | User dashboard is account-only |
| Scraper integration | Missing | Scrapers are standalone |
| Unified scraper output | Missing | Retailer-specific shapes |
| Docker/CI/CD | Missing | Local-only workflow |
| Database migrations | Missing | `create_all()` plus startup `ALTER TABLE` |
| Structured logging/metrics | Essentially missing | Uvicorn logs only |
| Automated E2E product flow | Missing | No product flow exists yet |

### Important distinction

A feature may be “implemented” and still have security or operational weaknesses.

Example: Google Sign-In is implemented, but automatic email linking creates a pre-hijacking risk.

---

## E. Dependency and Communication Map

### Backend internal dependencies

```text
main.py
  ├── database.py
  │     ├── .env loading
  │     ├── async engine
  │     └── AsyncSessionLocal / get_db
  ├── models.py
  │     ├── User
  │     ├── OTPChallenge
  │     ├── PasswordResetChallenge
  │     └── LoginEvent
  ├── rate_limit.py
  │     └── process-local SlidingWindowLimiter
  └── email_service.py
        ├── SMTP
        ├── Brevo
        ├── Resend
        └── Gmail API
```

### Frontend internal dependencies

```text
main.ts
  └── app.config.ts
        ├── provideRouter(routes)
        └── provideHttpClient(...)

app.routes.ts
  ├── Auth
  ├── ForgotPassword
  ├── Otp
  ├── UserDashboard
  └── AdminDashboard
        └── authGuard / adminGuard
              └── AuthServ
```

### Runtime communication

```text
Angular on localhost:4200
    |
    | HTTP + Bearer JWT
    v
FastAPI on localhost:8000
    |
    | asyncpg
    v
PostgreSQL
    |
    +----> Google token verification
    +----> Email provider APIs
```

### External services

| Service | Used by | Purpose |
|---|---|---|
| Google Identity | `/auth/google` | Verify Google ID tokens |
| SMTP provider | Email service | Send OTP emails |
| Brevo | Email service | HTTPS email delivery |
| Resend | Email service | HTTPS email delivery |
| Gmail API | Email service | OAuth-based email delivery |
| Retailer websites | Scrapers | Product data |

### Hard-coded boundaries

- Frontend API base: `http://localhost:8000` in `frontend/src/auth-serv.ts:78`.
- CORS allows only `localhost:4200` and `127.0.0.1:4200`.
- Scrapers commonly bind port `8000`.
- Main backend also uses port `8000`.
- No environment-based API base URL exists in the frontend.

---

## F. Architecture Reading Roadmap

A good order for understanding the codebase:

1. `frontend/src/auth-serv.ts` — see every backend operation the UI can call.
2. `backend/main.py` — read request schemas, helpers, then route groups.
3. `backend/models.py` — understand persistent state.
4. `backend/database.py` — understand session and engine setup.
5. `frontend/src/app/app.routes.ts` and `auth.guard.ts` — understand navigation.
6. One frontend feature at a time: auth, OTP, reset, dashboard, admin.
7. `backend/Webscrappers/telex.py` — read the simplest scraper first.
8. `backend/Webscrappers/daraz.py` — compare with the largest and most complex scraper.
9. `backend/tests/` — see intended behavior and security assertions.
10. Revisit the roadmap in Chapter 18 before proposing new features.

---

# Part II — Detailed Chapters

## Chapter 1 — Project Status and Purpose

### Purpose

The project appears intended to compare product prices across Pakistani retailers.

The current repository does not yet deliver that outcome. It delivers the account system that such a product would need, plus a set of scraper experiments that could eventually provide retailer data.

### What works today

- A user can create an account.
- A verification code can be sent by email.
- A user can verify the account.
- A user can log in with a password.
- A user can use Google Sign-In.
- A user can reset a password.
- A user can view basic account information.
- An admin can search, inspect, edit, block, unblock, and delete users.
- An admin can view successful login history.
- Blocked and unverified users are rejected by protected endpoints.

### What does not work today

- Searching products through the main application.
- Saving product or price data.
- Comparing offers from multiple retailers.
- Viewing price history.
- Triggering a scraper from the main backend.
- Admin audit trails or MFA.
- Revoking JWTs after password reset.

### Current maturity assessment

The auth subsystem is the strongest and most complete part of the project. It has meaningful tests and sensible security controls.

The scraper subsystem is exploratory. It has real extraction code, but it is not productionized, not integrated, and not uniformly structured.

The product domain is absent. This is the largest gap between the current repository and the stated product idea.

### The correct conclusion

> This is an authentication MVP with scraper prototypes, not yet a price-comparison product.

---

## Chapter 2 — Running the Project

### Prerequisites

- Python 3.11+
- Node.js 22+
- npm 10+
- PostgreSQL
- A configured `backend/.env`
- Internet access for Google and email providers
- Playwright browsers for scrapers

### Backend setup

From `backend/`:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

The backend requires:

- `DATABASE_URL`
- `JWT_SECRET_KEY`

It warns at startup if `OTP_HMAC_SECRET` is missing, and signup/reset will fail until it is configured.

### Frontend setup

From `frontend/`:

```powershell
npm install
npm start
```

The development server normally serves the app at:

```text
http://localhost:4200
```

### Scraper setup

From `backend/Webscrappers/`:

```powershell
python -m pip install playwright beautifulsoup4 lxml
python -m playwright install chromium
python telex.py
```

Each scraper is a separate FastAPI app. Most bind port `8000`, so they conflict with the main backend and with one another.

### Port map

| Component | Port |
|---|---|
| Angular development server | 4200 |
| Main FastAPI backend | 8000 |
| Scrapers | Usually 8000 |
| PostgreSQL | 5432 by default |

### Common run-order problem

Do not start a scraper and the main backend on the same port.

A successful scraper start can prevent the auth backend from starting, or vice versa.

### Useful verification commands

Backend:

```powershell
python -m unittest discover -s tests -v
```

Frontend type-check:

```powershell
npx tsc --noEmit -p tsconfig.app.json
```

Frontend build:

```powershell
npm run build
```

Frontend tests:

```powershell
npm test
```

Do not run database-mutating tests during a read-only review.

---

## Chapter 3 — Configuration and Secrets

Configuration is loaded from `backend/.env` in both `database.py` and `main.py`.

### Required variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | Async PostgreSQL connection URL |
| `JWT_SECRET_KEY` | Yes | JWT signing secret |
| `OTP_HMAC_SECRET` | For OTP flows | HMAC secret for OTP and reset-token hashing |

### JWT variables

| Variable | Default | Purpose |
|---|---|---|
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `JWT_EXPIRE_MINUTES` | `30` | Access-token lifetime |

### Google variable

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID` | Audience/client ID used to verify Google ID tokens |

### Admin variable

| Variable | Purpose |
|---|---|
| `ADMIN_EMAILS` | Comma-separated emails promoted to admin |

### OTP variable

| Variable | Default | Purpose |
|---|---|---|
| `OTP_RESEND_COOLDOWN_SECONDS` | `30` | Minimum delay between OTP resends |

### Email provider variables

`EMAIL_PROVIDER` selects one of:

```text
smtp
brevo
resend
gmail_api
```

SMTP variables:

```text
SMTP_HOST
SMTP_PORT
SMTP_USE_TLS
SMTP_USE_SSL
SMTP_USERNAME
SMTP_PASSWORD
SMTP_FROM_EMAIL
SMTP_FROM_NAME
```

Brevo variables:

```text
BREVO_API_KEY
BREVO_SENDER_EMAIL
BREVO_SENDER_NAME
```

Resend variables:

```text
RESEND_API_KEY
RESEND_FROM_EMAIL
RESEND_FROM_NAME
```

Gmail API variables:

```text
GMAIL_API_CLIENT_ID
GMAIL_API_CLIENT_SECRET
GMAIL_API_REFRESH_TOKEN
GMAIL_API_SENDER_EMAIL
GMAIL_API_SENDER_NAME
```

### Security rules for configuration

- Never commit the real `backend/.env`.
- Never log secrets or OTPs.
- Use different values for `JWT_SECRET_KEY` and `OTP_HMAC_SECRET`.
- Rotate secrets through a deployment process, not ad hoc edits.
- Do not paste real tokens or provider keys into debugging tools.

### Important limitation

`.env.example` says the JWT and OTP secrets must differ, but the application does not enforce inequality.

### Changing a secret has consequences

- Changing `JWT_SECRET_KEY` invalidates all existing JWTs.
- Changing `OTP_HMAC_SECRET` invalidates all outstanding OTPs and reset-token hashes.

---

## Chapter 4 — FastAPI Lifecycle and Request Pipeline

### Application creation

`backend/main.py` creates:

```python
app = FastAPI(title="Prices Indicator API")
```

It adds CORS middleware for the Angular development origins.

It defines an HTTP bearer scheme with `auto_error=False`, allowing custom handling of missing or invalid credentials.

### Startup behavior

The startup hook does four things:

1. Creates missing tables from ORM metadata.
2. Adds `users.is_blocked` if missing.
3. Adds `users.last_login_at` if missing.
4. Adds `users.last_login_method` if missing.

This is convenient but problematic:

- no migration history
- no rollback
- potential multi-worker races
- startup performs schema administration
- tests do not validate upgrade paths

### Request pipeline for protected routes

```text
HTTP request
    |
    v
CORS middleware
    |
    v
FastAPI dependency resolution
    |
    +--> get_db -> AsyncSession
    +--> get_current_user
            |
            +--> parse bearer token
            +--> decode JWT
            +--> load user from PostgreSQL
            +--> reject missing/blocked/unverified
            +--> optional admin promotion
    |
    v
Route function
    |
    v
Database transaction
    |
    v
JSON response or HTTPException
```

### Route groups

- `/auth/*` — signup, login, OTP, reset, Google, current user
- `/admin/*` — admin checks and user management
- `/` — basic root response

### Error style

Most errors use FastAPI `HTTPException` with a `detail` string. Some operations return custom `JSONResponse` objects because they need headers such as `Retry-After`.

### What is missing

- no `/health` endpoint
- no structured request logging
- no global exception handler
- no request ID propagation
- no rate-limit headers on every response
- no metrics
- no tracing

### Root endpoint

The root route is at `backend/main.py:1831`. It is informational only.

---

## Chapter 5 — Database Schema and Models

The ORM schema is defined in `backend/models.py`.

### Entity relationship overview

```text
users
  |
  +--< otp_challenges
  |
  +--< password_reset_challenges
  |
  +--< login_events
```

All three child relationships point to `users.id` with `ON DELETE CASCADE`.

### `User`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | Primary key |
| `name` | String(100) | Required |
| `email` | String(320) | Unique, indexed, required |
| `password_hash` | String(255) | Nullable for Google-only accounts |
| `google_sub` | String(255) | Unique, nullable |
| `email_verified` | Boolean | Default false |
| `role` | String(20) | Default `user` |
| `is_blocked` | Boolean | Default false |
| `last_login_at` | Timestamp with timezone | Nullable |
| `last_login_method` | String(20) | Nullable |
| `created_at` | Timestamp with timezone | Default UTC now |

### `OTPChallenge`

| Column | Purpose |
|---|---|
| `id` | Primary key |
| `challenge_id` | Random public challenge identifier |
| `user_id` | Owning user |
| `otp_hash` | HMAC hash of the OTP |
| `expires_at` | Expiry time |
| `attempts` | Failed attempt count |
| `consumed` | Whether the challenge is finished |
| `created_at` | Creation timestamp |

### `PasswordResetChallenge`

Adds reset-specific fields:

| Column | Purpose |
|---|---|
| `otp_verified` | Whether the OTP step succeeded |
| `reset_token_hash` | Hash of the reset token |
| `reset_token_expires_at` | Reset token expiry |

### `LoginEvent`

| Column | Purpose |
|---|---|
| `id` | Primary key |
| `user_id` | User who logged in |
| `method` | `password` or `google` |
| `logged_in_at` | Timestamp |

There is a composite index on `(user_id, logged_in_at)`.

### Database session behavior

`backend/database.py` creates an async engine with:

- `pool_pre_ping=True`
- `expire_on_commit=False`

`get_db()` yields an `AsyncSession` and closes it after the request.

### Transaction patterns

- Challenge rows are locked with `FOR UPDATE` in verification flows.
- Signup handles unique-email races with `IntegrityError`.
- Password reset consumes all outstanding reset challenges.
- Login history and last-login fields are updated on successful login.

### Time handling

The models use timezone-aware UTC timestamps. Comparisons should remain timezone-aware.

### Missing database capabilities

- no migration history
- no product tables
- no price-observation tables
- no audit-log table
- no token-revocation table
- no soft-delete pattern
- no explicit constraint naming strategy for most constraints

---

## Chapter 6 — Authentication Foundations

Three mechanisms form the foundation:

1. bcrypt password hashing
2. JWT access tokens
3. database-backed account-state checks

### bcrypt

Passwords are hashed with bcrypt and verified with the library’s constant-time comparison behavior.

Plaintext passwords are never stored.

### JWT creation

The token includes:

- `sub` — user ID as a string
- `iat` — issued-at time
- `exp` — expiry time

The default lifetime is 30 minutes.

The token is signed with `JWT_SECRET_KEY`.

### JWT storage

The Angular client stores the token in:

```text
localStorage["access_token"]
```

This is simple but exposes the token to JavaScript. In production, a hardened design might use secure, HTTP-only cookies with CSRF protection.

### Protected-request validation

`get_current_user`:

1. extracts the bearer token
2. decodes and verifies it
3. requires `sub`
4. converts `sub` to an integer
5. loads the user from PostgreSQL
6. rejects missing users
7. rejects blocked users
8. rejects unverified users
9. optionally promotes an allowlisted admin
10. returns the user

### Admin validation

`get_admin_user` builds on `get_current_user` and requires:

```text
role == "admin"
```

The role is read from the database, not trusted from token claims.

### Token limitations

- no refresh token
- no revocation list
- no token version
- no “logout all devices”
- no immediate invalidation after password reset
- no distinction between access and refresh credentials

### Why the design is still meaningful

Even without revocation, blocked and deleted users are rejected because every protected request reloads the user row.

The token is not the final authority on account status.

---

## Chapter 7 — Signup and Email OTP

### Endpoint

```text
POST /auth/signup
```

### Input

```json
{
  "name": "Test User",
  "email": "user@example.com",
  "password": "Password123!"
}
```

### Processing

1. Normalize email.
2. Enforce signup rate limit.
3. Check `OTP_HMAC_SECRET`.
4. Look up existing user.
5. For verified or blocked accounts, return a generic response.
6. For unverified accounts, update name/password and issue a new OTP.
7. For new accounts, hash the password and create the user.
8. Promote allowlisted admin emails.
9. Create an OTP challenge.
10. Send the OTP email.
11. Return a challenge ID and resend delay.

### OTP generation and storage

The OTP is:

- six digits
- generated securely
- hashed with HMAC using the challenge ID and `OTP_HMAC_SECRET`

The plaintext OTP is not stored.

### Response

New or unverified path:

```json
{
  "message": "Verification code sent to your email.",
  "challenge_id": "...",
  "resend_after_seconds": 30
}
```

No JWT is issued.

### Anti-enumeration design

A verified or blocked duplicate receives a generic response rather than a clear “account exists” message.

### Resend endpoint

```text
POST /auth/resend-otp
```

It:

- validates the challenge
- enforces a cooldown
- replaces old OTP challenges for that user
- sends a new code
- returns a new challenge ID

### Verify endpoint

```text
POST /auth/verify-otp
```

It:

- applies an IP rate limit
- locks the challenge row
- checks expiry and consumption
- checks attempt limits
- compares HMACs with `compare_digest`
- increments attempts on failure
- consumes the challenge after too many failures
- marks the user verified on success
- promotes allowlisted admins
- returns user data

No JWT is issued after verification.

### Security properties

- OTPs are not stored in plaintext.
- OTP challenges expire.
- Attempts are limited.
- Challenge rows are locked during verification.
- Error messages avoid account enumeration.
- Signup and resend are rate-limited.

### Weaknesses

- OTP delivery depends on an external email provider.
- Email failure can leave an unverified account behind.
- The challenge ID alone is a bearer-like secret during verification.
- There is no device binding or risk scoring.

---

## Chapter 8 — Login, Blocking, and Login History

### Endpoint

```text
POST /auth/login
```

### Processing order

1. Normalize email.
2. Enforce login rate limit.
3. Load user by email.
4. Verify that a password hash exists.
5. Verify the password with bcrypt.
6. Reject blocked accounts.
7. Reject unverified accounts.
8. Promote allowlisted admins.
9. Record a successful login event.
10. Update last-login fields.
11. Create a JWT.
12. Return the token and user data.

### Why order matters

Password verification happens before status checks.

Results:

- unknown email or wrong password → `401`
- correct password but blocked → `403`
- correct password but unverified → `403`

This preserves a generic response for credential failures while giving an explicit account-state response after a correct password.

### Login response

```json
{
  "message": "Login successful.",
  "access_token": "...",
  "token_type": "bearer",
  "user": {
    "id": 1,
    "name": "Test User",
    "email": "user@example.com",
    "role": "user",
    "email_verified": true
  }
}
```

### Login history

Only successful logins create `LoginEvent` rows.

The system stores:

- user ID
- method
- timestamp

It does not store:

- IP address
- user agent
- device
- location

### Blocking

Blocking sets `users.is_blocked = true`.

Existing tokens remain valid cryptographically, but protected requests fail because the user row is blocked.

### Unblocking

Unblocking sets the flag to false.

An old unexpired token can become usable again. This is an important limitation of the current model.

### Admin self-protection

Admins cannot block themselves.

Admins cannot block, demote, or delete the last active admin.

### Login rate limiting

The login limiter is keyed per IP and email within a one-minute window.

### Remaining risks

- no MFA
- no adaptive risk scoring
- no IP/device history
- no refresh-token rotation
- no token revocation

---

## Chapter 9 — Google Sign-In

### Endpoint

```text
POST /auth/google
```

### Input

```json
{
  "id_token": "google-id-token"
}
```

### Processing

1. Check that `GOOGLE_CLIENT_ID` is configured.
2. Verify the Google ID token server-side.
3. Require a verified Google email.
4. Find a user by `google_sub`.
5. If not found, find by email.
6. Reject a blocked account.
7. Create an account if none exists.
8. Reject a conflicting Google identity.
9. Link Google to an existing email account.
10. Mark the account verified.
11. Record a Google login.
12. Issue a JWT.

### New account behavior

A Google-created account may have no password hash. It can still log in through Google.

### Linking behavior

If an email already exists, the Google subject can be attached to that account.

This creates a security-sensitive pre-hijacking risk:

1. An attacker creates an unverified password account with a victim’s email.
2. The victim later uses Google with that email.
3. The backend links Google to the existing account.
4. The attacker may still retain password access.

A safer design would require proof of ownership before linking, or refuse automatic linking to unverified password accounts.

### Google-only account risks

- Email edits by an admin can detach or confuse identity.
- Clearing `google_sub` while leaving no password can lock the account out.
- There is no notification when a new Google identity is linked.

---

## Chapter 10 — Password Reset

Password reset has three steps.

### Step 1: request reset

```text
POST /auth/forgot-password
```

Behavior:

- normalizes email
- applies rate limiting
- returns a generic response
- silently ignores unknown, blocked, or Google-only accounts
- creates a reset challenge for eligible accounts
- sends an OTP

The response includes a generated `challenge_id` even for unknown accounts. This keeps response shapes similar and reduces enumeration signals.

### Step 2: resend reset OTP

```text
POST /auth/resend-password-reset
```

It applies cooldown and challenge checks, replaces prior OTP challenges, and sends a new code.

### Step 3: verify reset OTP

```text
POST /auth/verify-password-reset-otp
```

It checks:

- challenge exists
- not consumed
- OTP not already verified
- not expired
- attempts remaining
- HMAC match
- user exists and is not blocked
- user still has a password

On success it:

- creates a random reset token
- stores only its hash
- sets a ten-minute expiry
- marks OTP verified
- returns the reset token

### Step 4: set new password

```text
POST /auth/reset-password
```

It:

1. hashes the submitted reset token
2. finds the challenge
3. checks expiry and verification
4. loads the user
5. hashes the new password
6. consumes the authorizing challenge
7. consumes all other outstanding reset challenges for the user
8. commits

### Important limitation

Existing JWTs are not revoked.

A user who resets a compromised password may still have an attacker session active until token expiry or account block.

### Missing notification

No email is sent after a password change.

---

## Chapter 11 — Admin User Management

### Admin check

```text
GET /admin/check
```

Requires an authenticated admin.

### List users

```text
GET /admin/users
```

Supports:

- search by name or email
- numeric search by exact user ID
- status filters: all, active, blocked, inactive
- pagination

Response shape:

```json
{
  "items": [],
  "total": 0,
  "page": 1,
  "page_size": 10,
  "pages": 1
}
```

### Detail

```text
GET /admin/users/{user_id}
```

Returns safe admin-facing user data.

### Login history

```text
GET /admin/users/{user_id}/logins
```

Returns paginated successful login events with method and timestamp.

### Update user

```text
PATCH /admin/users/{user_id}
```

API supports:

- name
- email
- role

The dashboard UI exposes name and email, but not role editing.

### Block/unblock

```text
POST /admin/users/{user_id}/block
POST /admin/users/{user_id}/unblock
```

Protections:

- no self-block
- no blocking the last active admin

### Delete

```text
DELETE /admin/users/{user_id}
```

Protections:

- no self-delete
- no deleting the last active admin

### Important weaknesses

- No audit log.
- No admin MFA.
- No admin password reset.
- No create-user endpoint.
- Last-admin protection is application-level and can theoretically race.
- Editing a Google-only account can create lockout states.

### Safe response data

The admin-facing response excludes:

- password hashes
- Google subject identifiers
- OTP data
- tokens

---

## Chapter 12 — User Dashboard, Routing, and Session Handling

### Routes

```text
/auth             -> Auth
/forgot-password  -> ForgotPassword
/otp              -> Otp
/dashboard        -> UserDashboard, authGuard
/admin            -> AdminDashboard, authGuard + adminGuard
/                 -> redirect to /auth
/**               -> redirect to /auth
```

### Guards

`authGuard` only checks that a token exists.

`adminGuard`:

- checks token existence
- calls `/auth/me`
- allows only role `admin`
- clears the session and redirects on failure

The backend remains the real authority.

### AuthServ

`frontend/src/auth-serv.ts` is the central HTTP client. It provides methods for:

- signup
- login
- OTP verification
- OTP resend
- forgot password
- reset OTP resend
- reset OTP verification
- password reset
- Google login
- current user
- admin check
- admin user list/detail/logins
- admin update/block/unblock/delete

It stores:

```text
localStorage["access_token"]
```

and temporary OTP/reset state in `sessionStorage`.

### User dashboard

The user dashboard shows account information only.

It does not display products, prices, comparisons, or saved searches.

### Error handling

- `401` typically leads to session clearing/login.
- Blocked `403` can appear as a generic retry error.
- Admin guard redirects non-admins to `/dashboard`.

### Frontend build issues

`frontend/src/main.ts` contains dead trailing code:

```ts
providers: [provideRouter(routes)]
```

after the bootstrap expression. It is not valid as intended provider registration and serves no purpose.

### Bundle size

The production build warning is:

```text
520.19 kB exceeds the 500 kB budget
```

This is a performance concern, not a functional failure.

### Hard-coded API URL

The client uses:

```ts
private readonly apiUrl = 'http://localhost:8000';
```

There is no environment configuration for production.

---

## Chapter 13 — The Scraper Subsystem

The scrapers live under `backend/Webscrappers/`.

Each file is a standalone FastAPI app. None is imported by the main backend or frontend.

### Inventory

| Scraper | Lines | Mode | Notes |
|---|---:|---|---|
| `alfatah.py` | 901 | Async Playwright | No reviews |
| `daraz.py` | 2836 | Sync Playwright | Largest, reviews, detail enrichment |
| `mega.py` | 620 | Sync Playwright | v5, warranty/debug options |
| `naheed.py` | 853 | Sync Playwright | v3, keyword and limit |
| `priceoye.py` | 1043 | Async Playwright | v6.3.1, reviews, concurrency |
| `shopon.py` | 873 | CS-Cart | Compact output, `/api/debug` |
| `telex.py` | 1016 | Shopify API | v2, `/products/<handle>.js` |

### Common flow

```text
HTTP request
    |
    v
Build retailer search URL
    |
    v
Launch browser
    |
    v
Load search page
    |
    v
Wait / scroll
    |
    v
Extract product cards
    |
    v
Optionally enrich detail pages
    |
    v
Optionally collect reviews
    |
    v
Deduplicate and sort
    |
    v
Return JSON
```

### What the scrapers do not share

- no canonical product schema
- no shared error policy
- no shared output model
- no shared authentication
- no shared persistence
- no shared orchestration
- no shared tests

### Ports and conflicts

Most bind `8000`, conflicting with:

- the main backend
- other scrapers

### Deployment problems

- Playwright requires browser binaries.
- Retailer sites can block automation.
- Selectors drift.
- Long-running requests may time out.
- Some scrapers return partial results on failure.
- Some expose raw error details.
- CORS is usually permissive.
- There is no authentication on scraper APIs.

### Telex

Telex is the most integration-friendly candidate because it uses a Shopify JSON endpoint rather than relying entirely on DOM structure.

### Daraz

Daraz is the most complex. It includes detail enrichment and reviews, making it a poor first integration target.

### Why the scrapers feel finished but are not product-ready

They can return useful JSON in ideal conditions. They lack the surrounding engineering needed for reliable, comparable, persistent price data.

---

## Chapter 14 — Product-Comparison Integration Design

This chapter describes a proposed design. None of it is implemented.

### Goal

Turn multiple retailer responses into comparable product offers.

### Canonical offer model

A normalized offer should include:

```text
retailer
retailer_product_id
title
url
image_url
price
currency
availability
rating
review_count
scraped_at
raw_title
match_confidence
```

### Proposed tables

```text
products
  id
  canonical_name
  brand
  model
  category
  created_at

retailer_products
  id
  product_id
  retailer
  retailer_product_id
  title
  url
  image_url
  last_seen_at

price_observations
  id
  retailer_product_id
  price
  currency
  availability
  observed_at

scrape_runs
  id
  retailer
  query
  started_at
  finished_at
  status
  product_count
  error_message
```

### Orchestration

```text
Product search request
    |
    v
Orchestrator
    |
    +--> retailer adapter: Telex
    +--> retailer adapter: Daraz
    +--> retailer adapter: ...
    |
    v
Normalize
    |
    v
Validate
    |
    v
Match to canonical products
    |
    v
Persist observations
    |
    v
Group offers
    |
    v
Return comparison response
```

### Retailer adapter contract

Each adapter should:

- accept a query and limit
- enforce its own timeout
- return normalized offers
- report retailer status
- avoid leaking raw internal errors to clients
- be independently testable

### Partial failure

A comparison search should not fail completely because one retailer times out.

A response should distinguish:

- successful retailers
- empty results
- timeouts
- parse failures
- blocked requests

### Matching

Exact string matching is insufficient. A real matcher needs:

- normalization
- brand/model extraction
- size and variant handling
- fuzzy similarity
- confidence thresholds
- manual review for ambiguous matches

### Recommended first vertical slice

Start with one retailer, most likely Telex.

Build:

1. adapter
2. canonical offer schema
3. persistence
4. orchestration
5. comparison API
6. minimal search/comparison UI
7. tests
8. failure handling

Only then add more retailers.

### Why not connect all seven immediately

Seven unreliable integrations create a large surface area:

- inconsistent data
- more failure modes
- more debugging
- no proven domain model
- more time before a usable feature exists

One narrow vertical slice proves the architecture.

---

## Chapter 15 — Testing

### Backend

Test files:

```text
backend/tests/test_auth_flow.py              57 tests
backend/tests/test_admin_user_management.py   8 tests
backend/tests/test_password_reset.py         12 tests
```

Total: 77 tests.

### Test style

- `unittest.IsolatedAsyncioTestCase`
- no `conftest.py`
- live PostgreSQL required
- unique temporary schemas
- `search_path` isolation
- `Base.metadata.create_all`
- `get_db` override
- schema dropped with `CASCADE` afterward

### What is well covered

- signup
- OTP verification
- OTP resend
- login
- blocked/unverified enforcement
- JWT behavior
- Google login paths
- email behavior
- rate limiting
- admin authorization
- user editing
- blocking/unblocking
- deletion protections
- login history
- password reset security

### What is not covered

- scraper behavior
- product domain
- orchestration
- migration upgrades
- real browser E2E flows
- production deployment behavior
- full frontend Karma result
- load and concurrency behavior

### Frontend

Approximately 25 `it(...)` assertions across 7 spec files were identified.

The final Karma result was not confirmed.

### Build verification

Type-check passed.

Production build passed with the bundle warning.

### Important testing limitation

The backend tests validate application behavior against a schema generated from the current models. They do not validate migrations from an older schema.

---

## Chapter 16 — Deployment and Operations

### Current state

The project is set up for local development.

Missing:

- Docker
- Compose
- CI/CD
- reverse proxy
- TLS
- process manager
- secrets manager
- centralized logging
- metrics
- alerts
- database backup automation
- migration pipeline

### Deployment blockers

1. Hard-coded frontend API URL.
2. CORS restricted to localhost.
3. No production environment configuration.
4. No migrations.
5. Startup DDL.
6. Token stored in `localStorage`.
7. Unpinned dependencies.
8. Scrapers not deployable as a coordinated service.
9. No health endpoint.
10. No secrets management.

### Rate limiting

The limiter is in-process and in-memory.

Consequences:

- resets on restart
- not shared across workers
- multiple workers weaken limits
- proxy IP handling is unresolved
- `request.client.host` may not represent the real client behind a proxy

### Operational logging

Uvicorn logs exist, but there is no structured application logging, request correlation, or security event log.

### Email operations

Email delivery depends on an external provider and can fail independently of the database.

### Database operations

There is no documented backup/restore procedure and no migration process.

### Recommended deployment shape

At minimum:

```text
Reverse proxy / TLS
    |
    v
Application server (one or more workers)
    |
    v
Managed PostgreSQL
    |
    +--> Email provider
    +--> Google Identity
```

Scrapers should run as a separate worker/service, not inside the API process.

---

## Chapter 17 — Security Review

### Strengths

- bcrypt password hashing
- JWT signature verification
- Google ID-token verification server-side
- OTP HMAC hashing
- constant-time OTP comparison
- OTP expiry and attempt limits
- challenge row locking
- rate limiting on sensitive routes
- generic responses for account enumeration resistance
- database role checks on every admin request
- last-active-admin and self-protection
- safe admin response fields
- no plaintext OTP storage
- reset tokens stored as hashes
- reset token single-use consumption

### Significant risks

#### 1. Google account pre-hijacking

Automatic linking to an existing unverified password account can let an attacker retain access.

#### 2. No JWT revocation

Password reset, logout, and account changes do not invalidate outstanding tokens.

#### 3. Token storage in `localStorage`

Any successful XSS can read the token.

No production CSP is defined.

#### 4. No admin MFA or audit log

Admin compromise or misuse is harder to detect and contain.

#### 5. Unauthenticated scrapers

Scraper APIs can be called by anyone who can reach them.

#### 6. Unpinned dependencies

Builds are not reproducible and supply-chain drift is possible.

#### 7. No TLS or security headers

Local development is plain HTTP. Production hardening is absent.

#### 8. Email enumeration through timing

Generic responses reduce enumeration, but timing and delivery behavior can still leak information.

#### 9. Rate limiting is process-local

It is not a reliable defense in a multi-worker deployment.

#### 10. No account-change notifications

Users are not notified of password changes or new Google links.

### Security priorities

1. Fix Google pre-hijacking.
2. Add token revocation or short-lived tokens with refresh rotation.
3. Add admin MFA and audit logging.
4. Move to secure cookie-based sessions or harden token storage with CSP.
5. Add scraper authentication and network restrictions.
6. Pin dependencies.
7. Add production TLS/security headers.
8. Add account-change notifications.
9. Add centralized security logging.
10. Add migration and backup controls.

---

## Chapter 18 — Final Assessment and Roadmap

### Overall assessment

Prices Indicator currently consists of:

- a credible authentication MVP
- a functional admin surface
- a basic user dashboard
- seven experimental scrapers
- no product domain
- no integration layer
- no comparison UI

### Strongest parts

- auth flow completeness
- meaningful backend tests
- sensible account-state enforcement
- clear model relationships
- admin safety checks

### Weakest parts

- no product comparison
- no scraper integration
- no migrations
- no deployment pipeline
- token lifecycle limitations
- Google linking risk

### Recommended roadmap

#### Phase 1: stabilize authentication

- fix Google linking
- add token revocation or refresh rotation
- add audit logging
- add account-change emails
- add admin MFA

#### Phase 2: productionize operations

- migrations
- environment-based frontend configuration
- CI/CD
- containerization
- health/readiness checks
- structured logging
- backups
- pinned dependencies

#### Phase 3: build one vertical product slice

- choose Telex
- define canonical offer model
- add product/offer/price tables
- build adapter contract
- orchestrate one retailer
- build search and comparison API
- build minimal UI
- test end to end

#### Phase 4: expand retailers

- add adapters one at a time
- add matching and deduplication
- add retries and scheduled scraping
- add price history
- add alerts

#### Phase 5: scale

- worker queues
- caching
- observability
- rate limiting at the edge
- data quality monitoring

### Definition of done for the first product slice

- a user can search for a product
- at least one retailer returns normalized offers
- offers are persisted
- the UI displays comparable results
- failures are shown cleanly
- tests cover the flow
- the feature works without manual database edits

---

## Chapter 19 — End-to-End Walkthroughs

This chapter follows real request paths.

### 19.1 Request path map

```text
Angular component
    |
    v
AuthServ HTTP request
    |
    v
FastAPI endpoint
    |
    +--> validation
    +--> rate limiting
    +--> PostgreSQL
    +--> email or Google
    |
    v
JSON response
    |
    v
Angular state update
    |
    +--> token storage
    +--> guarded navigation
```

### 19.2 Signup walkthrough

1. Browser submits name, email, password.
2. Backend normalizes and rate-limits.
3. Backend checks OTP configuration.
4. Backend finds or creates the user.
5. Backend creates an OTP challenge.
6. Backend sends the OTP email.
7. Backend returns a challenge ID.
8. No JWT is issued.

Database state:

```text
users: new/reused account, email_verified = false
otp_challenges: hashed OTP, expiry, attempts = 0, consumed = false
```

### 19.3 OTP verification walkthrough

1. Browser sends challenge ID and OTP.
2. Backend locks the challenge row.
3. Backend checks consumption, expiry, and attempts.
4. Backend compares HMACs.
5. Backend marks the challenge consumed.
6. Backend sets `email_verified = true`.
7. Backend returns user data.
8. No JWT is issued.

### 19.4 Login walkthrough

1. Browser sends email and password.
2. Backend rate-limits.
3. Backend loads the user.
4. Backend verifies bcrypt hash.
5. Backend checks blocked status.
6. Backend checks verification status.
7. Backend records login event and updates last-login fields.
8. Backend issues JWT.
9. Frontend stores token and navigates.

### 19.5 Protected request walkthrough

1. Browser sends bearer token.
2. Backend decodes JWT.
3. Backend loads user from PostgreSQL.
4. Backend rejects missing, blocked, or unverified users.
5. Backend returns the resource.

This is why blocking is immediate despite stateless tokens.

### 19.6 Admin block walkthrough

1. Admin submits block action.
2. Backend verifies admin role.
3. Backend loads target.
4. Backend rejects self-block and last-admin block.
5. Backend sets `is_blocked = true`.
6. Target’s next protected request returns `403`.

Unblocking can reactivate an unexpired old token.

### 19.7 Google walkthrough

1. Frontend obtains Google ID token.
2. Backend verifies it.
3. Backend requires verified email.
4. Backend looks up `google_sub`, then email.
5. Backend creates or links the account.
6. Backend marks verified.
7. Backend records login and issues JWT.

The linking step carries pre-hijacking risk.

### 19.8 Password reset walkthrough

1. User requests reset.
2. Backend returns a generic response.
3. Eligible users receive an OTP challenge.
4. User verifies the OTP.
5. Backend returns a single-use reset token.
6. User submits token and new password.
7. Backend consumes all outstanding reset challenges.
8. Existing JWTs remain valid.

### 19.9 Scraper walkthrough

1. Caller hits a standalone scraper.
2. Scraper builds a retailer URL.
3. Playwright loads the page.
4. Scraper extracts products.
5. Scraper optionally enriches details and reviews.
6. Scraper returns JSON.
7. No auth database write occurs.

### 19.10 Future product-search walkthrough

1. Angular search page calls main backend.
2. Backend checks cache.
3. Backend calls one or more retailer adapters.
4. Adapters return normalized offers or statuses.
5. Backend validates and matches products.
6. Backend persists scrape run and price observations.
7. Backend returns grouped offers.
8. UI renders comparison.

This flow does not exist yet.

### 19.11 Failure-state map

| Action | Failure | Result |
|---|---|---|
| Signup | missing OTP secret | `503` |
| Signup | email provider failure | generic `503`; account remains unverified |
| OTP verify | wrong code | `400`; attempts increase |
| OTP verify | too many attempts | challenge consumed, `429` |
| OTP verify | expired code | `400` |
| Login | wrong password | `401` |
| Login | blocked | `403` |
| Login | unverified | `403` |
| Protected request | expired token | `401` |
| Protected request | blocked | `403` |
| Protected request | deleted user | `401` |
| Admin action | non-admin | `403` |
| Admin block | self-block | `400` |
| Admin block | last active admin | `409` |
| Forgot password | unknown email | generic `200` |
| Reset | invalid/expired token | `400` |
| Scraper | timeout | often `504` |
| Scraper | parse/browser failure | often `500` or `502` |

### 19.12 Mental model

Authentication is a relay race with a full track:

```text
browser -> validation -> limiter -> database -> email/Google -> token -> guard
```

A scraper request stops at JSON.

The product-comparison relay has not been built.

---

## Chapter 20 — Debugging Playbook

### Core rule

Find the first layer where reality differs from expectation, then inspect only that layer.

### Five layers

```text
Environment
Transport
Application
Persistence
Browser
```

### Startup checks

```powershell
python --version
node --version
npm --version
```

Check required configuration presence without printing values.

Required:

```text
DATABASE_URL
JWT_SECRET_KEY
```

For OTP:

```text
OTP_HMAC_SECRET
```

For Google:

```text
GOOGLE_CLIENT_ID
```

### Backend startup problems

#### `ModuleNotFoundError`

Likely causes:

- dependencies not installed
- wrong working directory
- wrong Python environment
- package missing from requirements

Check installed packages:

```powershell
python -m pip show fastapi sqlalchemy asyncpg uvicorn
```

Install declared dependencies:

```powershell
python -m pip install -r requirements.txt
```

#### Missing environment variable

The backend intentionally refuses to start without `DATABASE_URL` or `JWT_SECRET_KEY`.

#### Database connection refused

Check:

- PostgreSQL running
- host and port correct
- database exists
- firewall/VPN
- container state

#### PostgreSQL authentication failure

Check:

- user
- password
- database name
- role permission

Do not weaken authentication to work around it.

#### Startup `ALTER TABLE` permission failure

The app database user needs schema-alter permission. This is a deployment design problem, not a reason to grant superuser.

#### Port already in use

Check for:

- main backend
- scraper
- another process

Most scrapers conflict with the backend on port `8000`.

### Frontend connection problems

#### `ERR_CONNECTION_REFUSED`

Check:

- backend running
- port
- hard-coded API URL
- HTTP vs HTTPS

#### CORS error

Allowed origins:

```text
http://localhost:4200
http://127.0.0.1:4200
```

A CORS failure happens in the browser. The request may never reach FastAPI.

#### `404`

Check:

- path
- trailing slash
- using a scraper route on the main backend
- calling an unimplemented product endpoint

#### Works in `curl`, fails in Angular

Check:

- payload shape
- CORS
- response field names
- token storage
- stale build

### Signup/OTP debugging

- `503`: missing OTP secret or email delivery failure
- no email: provider credentials, spam, rate limit, network
- OTP rejected: wrong challenge, consumed, expired, attempts, secret mismatch
- verified but unverified login: inspect `users.email_verified`

### Login debugging

- `401`: unknown email, wrong password, missing hash, expired token
- `403`: blocked or unverified
- redirect after success: token not stored or cleared
- admin route failure: database role

### Password-reset debugging

- generic success is intentional
- challenge ID does not prove account existence
- reset token rejection: expiry, consumption, copy error, secret change
- old session still works: known lack of JWT revocation

### Admin debugging

- `403`: role, verification, blocked state, missing token
- cannot block self: intentional
- cannot block last admin: intentional
- stale list: refresh
- empty history: only successful logins recorded

### Database debugging

Ask read-only questions:

```text
Does the user exist?
Is email_verified true?
Is is_blocked false?
What is the role?
Does the challenge exist?
Is it consumed or expired?
Was a login event created?
```

Do not mutate data during diagnosis unless allowed.

### Scraper debugging

- install Playwright and Chromium separately
- use one service per port
- zero products usually means selector drift, bot challenge, or incomplete page load
- error behavior varies by scraper
- debug HTML helps for some scrapers

### Test debugging

Backend tests need live PostgreSQL and temporary-schema permissions.

Do not weaken security assertions to make tests pass.

### Frontend build debugging

Read the first TypeScript error.

The bundle warning is not a functional failure.

### Disciplined sequence

1. Reproduce once.
2. Identify the exact action.
3. Check browser network.
4. Check backend terminal.
5. Read the safe error.
6. Check the relevant database row.
7. Form one hypothesis.
8. Make one minimal correction.
9. Re-test.
10. Record the cause.

### Symptom-to-layer map

| Symptom | First layer |
|---|---|
| Backend exits immediately | Environment |
| CORS error | Transport |
| `404` on known route | Transport/path |
| Login `401` | Credentials |
| Login `403` | Account state |
| Protected `401` | Token |
| Protected `403` | Blocked/unverified |
| Admin `403` | Role/account state |
| OTP rejected | Challenge state |
| Reset token rejected | Challenge state |
| Missing table/column | Persistence |
| Scraper zero results | Retailer/selectors |
| Scraper port conflict | Environment |

---

## Chapter 21 — Database Migrations and Safe Schema Evolution

### Current lifecycle

On startup, the backend:

1. creates missing tables from ORM metadata
2. adds three user columns with `ALTER TABLE ... IF NOT EXISTS`

It does not alter existing columns, rename columns, backfill data, or preserve history.

### Why `create_all()` is insufficient

`create_all()` creates missing tables. It does not make an existing schema match the models.

If a model gains a column, an existing table remains unchanged.

### Why startup DDL is risky

- multiple workers may race
- startup becomes an administration step
- no history
- no review boundary
- no rollback
- tests do not validate upgrades

### What a migration is

A versioned, ordered schema change with:

- revision ID
- parent revision
- description
- upgrade steps
- optional downgrade

### Alembic

Alembic is the standard SQLAlchemy migration tool.

Target structure:

```text
alembic.ini
backend/migrations/
    env.py
    versions/
        0001_initial_auth_schema.py
```

Autogeneration drafts migrations. Every draft must be reviewed.

### Baseline problem

Fresh database:

- baseline migration creates the schema

Existing database:

- verify schema matches models
- record baseline as applied
- do not recreate tables
- do not stamp blindly if drift exists

### Three change types

- Additive: nullable columns, new tables, new indexes
- Transformative: renames, type changes, backfills
- Destructive: drops, tightening nullability, deleting data

### Expand-and-contract

For risky renames:

1. add new nullable column
2. write both old and new
3. backfill
4. switch reads
5. enforce constraints
6. stop writing old column
7. drop old column later

### PostgreSQL safety notes

- adding a constant-default column is often cheap in modern versions, but verify
- `SET NOT NULL` can scan and lock
- `CREATE INDEX CONCURRENTLY` reduces blocking but cannot run inside a normal transaction
- long migrations can block writes
- name constraints explicitly

### Deployment order

1. back up
2. review SQL
3. run migration once as a dedicated job
4. deploy compatible app version
5. smoke test
6. monitor

Do not run migrations from every web worker.

### Target startup

Startup should load config, verify settings, create the connection pool, and serve.

A separate migration job should apply schema changes.

### Safe project workflow

1. inventory databases
2. add Alembic
3. generate and review baseline
4. verify/stamp existing databases
5. move startup patches into migrations
6. remove startup DDL only after the workflow is proven
7. test fresh install and upgrade paths
8. document the release command

### Migration testing

Test three things:

- application behavior
- fresh database from zero to head
- upgrade from previous revision with data preserved

Current tests do not prove migration safety.

### Data migrations

Schema changes and data backfills are different concerns.

Backfills need batching, idempotency, progress tracking, and verification.

### Rollback reality

Usually reversible:

- add nullable column
- add table
- add index

Risky or irreversible:

- drop column
- type change
- delete data
- destructive backfill

A downgrade function is not a substitute for a backup.

### Common mistakes

- trusting autogeneration
- running migrations from app workers
- mixing schema and large data changes
- adding `NOT NULL` without backfill
- dropping columns still in use
- one-step renames during rolling deployment
- assuming tests cover migrations
- stamping without verification
- no backup
- leaving `create_all()` in production forever

### Review checklist

```text
[ ] What changes?
[ ] Additive, transformative, or destructive?
[ ] Table sizes?
[ ] Locks?
[ ] Old app compatible during change?
[ ] New app compatible before change?
[ ] Backfill?
[ ] Batched and idempotent?
[ ] Named constraints/indexes?
[ ] Backup?
[ ] Real rollback plan?
[ ] SQL reviewed?
[ ] Tested on a copy?
```

### Recommended sequence for this project

1. baseline the auth schema
2. verify existing databases
3. move startup DDL into migrations
4. remove startup DDL
5. require migrations for all future changes
6. add product tables only through reviewed migrations

---

# Appendices

## Appendix A — Endpoint Reference

### Auth

| Method | Path | Purpose | Auth |
|---|---|---|---|
| POST | `/auth/signup` | Create or refresh unverified account | No |
| POST | `/auth/login` | Password login | No |
| POST | `/auth/resend-otp` | Resend signup OTP | No |
| POST | `/auth/verify-otp` | Verify signup OTP | No |
| POST | `/auth/forgot-password` | Start password reset | No |
| POST | `/auth/resend-password-reset` | Resend reset OTP | No |
| POST | `/auth/verify-password-reset-otp` | Verify reset OTP | No |
| POST | `/auth/reset-password` | Set new password | Reset token |
| POST | `/auth/google` | Google Sign-In | Google ID token |
| GET | `/auth/me` | Current user | Bearer JWT |

### Admin

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/check` | Verify admin access |
| GET | `/admin/users` | List/search/filter users |
| GET | `/admin/users/{user_id}` | User detail |
| GET | `/admin/users/{user_id}/logins` | Login history |
| PATCH | `/admin/users/{user_id}` | Update name/email/role |
| POST | `/admin/users/{user_id}/block` | Block user |
| POST | `/admin/users/{user_id}/unblock` | Unblock user |
| DELETE | `/admin/users/{user_id}` | Delete user |

### Root

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Informational root response |

### Scrapers

Scraper routes vary by file. They are standalone services, not part of the main backend.

---

## Appendix B — Environment Variables

### Core

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL async URL |
| `JWT_SECRET_KEY` | Yes | Must be strong and secret |
| `JWT_ALGORITHM` | No | Default `HS256` |
| `JWT_EXPIRE_MINUTES` | No | Default `30` |
| `OTP_HMAC_SECRET` | For OTP | Must differ from JWT secret |
| `GOOGLE_CLIENT_ID` | For Google | Web client ID |
| `ADMIN_EMAILS` | No | Comma-separated admin emails |
| `OTP_RESEND_COOLDOWN_SECONDS` | No | Default `30` |

### Email

| Variable | Used by |
|---|---|
| `EMAIL_PROVIDER` | Selects provider |
| `SMTP_HOST` | SMTP |
| `SMTP_PORT` | SMTP |
| `SMTP_USE_TLS` | SMTP |
| `SMTP_USE_SSL` | SMTP |
| `SMTP_USERNAME` | SMTP |
| `SMTP_PASSWORD` | SMTP |
| `SMTP_FROM_EMAIL` | SMTP |
| `SMTP_FROM_NAME` | SMTP |
| `BREVO_API_KEY` | Brevo |
| `BREVO_SENDER_EMAIL` | Brevo |
| `BREVO_SENDER_NAME` | Brevo |
| `RESEND_API_KEY` | Resend |
| `RESEND_FROM_EMAIL` | Resend |
| `RESEND_FROM_NAME` | Resend |
| `GMAIL_API_CLIENT_ID` | Gmail API |
| `GMAIL_API_CLIENT_SECRET` | Gmail API |
| `GMAIL_API_REFRESH_TOKEN` | Gmail API |
| `GMAIL_API_SENDER_EMAIL` | Gmail API |
| `GMAIL_API_SENDER_NAME` | Gmail API |

---

## Appendix C — Data Model Reference

### Relationship summary

```text
User 1 ---- * OTPChallenge
User 1 ---- * PasswordResetChallenge
User 1 ---- * LoginEvent
```

All child foreign keys use `ON DELETE CASCADE`.

### State transitions

#### User

```text
created
  |
  +--> email_verified = false
  |
  +--> email_verified = true
  |
  +--> is_blocked = true
  |
  +--> deleted
```

#### Signup OTP challenge

```text
created -> consumed on success
        -> consumed after too many failures
        -> expired but not consumed
```

#### Password reset challenge

```text
created
  |
  +--> otp_verified = true
  |
  +--> reset_token issued
  |
  +--> consumed after password change
```

#### Login event

```text
created only after successful password or Google login
```

---

## Appendix D — Commands

### Backend

From `backend/`:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

Tests:

```powershell
python -m unittest discover -s tests -v
```

Run tests only when database mutation is allowed.

### Frontend

From `frontend/`:

```powershell
npm install
npm start
npm test
npm run build
npx tsc --noEmit -p tsconfig.app.json
```

### Scrapers

From `backend/Webscrapers/`:

```powershell
python -m pip install playwright beautifulsoup4 lxml
python -m playwright install chromium
python telex.py
```

Only one service should use port `8000`.

---

## Appendix E — Glossary

| Term | Meaning |
|---|---|
| Access token | Short-lived credential proving authentication |
| JWT | Signed token containing claims such as user ID and expiry |
| bcrypt | Password-hashing algorithm |
| HMAC | Keyed hash used here for OTP and reset-token hashing |
| OTP | One-time password |
| Challenge | Server-side record that validates an OTP or reset step |
| Rate limit | Restriction on request frequency |
| Guard | Angular route check before navigation |
| Adapter | Proposed retailer-specific integration component |
| Canonical offer | Normalized product offer with a shared schema |
| Matching | Deciding which retailer offers refer to the same product |
| Migration | Versioned database schema change |
| Expand-and-contract | Staged pattern for safe schema changes |
| Pre-hijacking | Attack where an account is created before the victim claims it |
| Token revocation | Invalidating an issued token before expiry |

---

## Appendix F — Known Limitations and Verification Boundaries

### Not verified

- live PostgreSQL schema and data
- current scraper success against live retailer pages
- final Karma unit-test result
- production build deployment behavior
- multi-worker rate limiting
- proxy IP behavior
- email provider deliverability
- Google client configuration in the real environment

### Known implementation limitations

- no product domain
- no scraper integration
- no product UI
- no migrations
- no token revocation
- no refresh tokens
- no admin MFA
- no audit log
- hard-coded frontend API URL
- localhost-only CORS
- process-local rate limiting
- no health endpoint
- no structured logging
- unpinned backend dependencies
- scraper dependency gaps
- deprecated startup hook
- dead code in `frontend/src/main.ts`
- Google account-linking risk
- old JWTs remain valid after password reset

### Security-sensitive guidance

- Do not print `.env` contents.
- Do not paste real JWTs, OTPs, passwords, or API keys into logs or chat.
- Do not disable authentication or rate limiting to debug.
- Do not run destructive database commands without explicit approval.
- Do not treat passing tests as proof of production readiness.

### Final summary

The project has a solid authentication foundation and a promising set of scraper experiments.

It does not yet have the product domain, integration layer, persistence model, deployment process, or migration discipline required for a real price-comparison service.

The shortest path to a meaningful product is one reliable retailer, one canonical offer schema, one end-to-end search flow, and only then broader retailer coverage.

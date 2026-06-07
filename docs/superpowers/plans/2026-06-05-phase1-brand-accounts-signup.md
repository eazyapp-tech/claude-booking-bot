# Phase 1 — Plan 1: Brand Accounts + Self-Serve Signup + Demo + Native Login

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A brand can self-sign-up (email + password) and instantly get a working demo bot on sandbox inventory, then log back in — with zero code edits or redeploys.

**Architecture:** A per-brand account record in Redis (`brand_account:{email}`) holds a generated brand API key. Signup provisions a demo `brand_config` (via the existing `set_brand_config`) pointing at sandbox `pg_ids`, then stores the account. Native login looks up the account, constant-time-checks the password, and returns the brand's API key — which the admin panel already sends as `X-API-Key`. This **reuses the entire existing `require_admin_brand_key` machinery unchanged** and solves the chicken-and-egg (signup is public; it *creates* the brand). Email verification is issued at signup (token in Redis) and gates go-live later (Plan 2), not the demo.

**Tech Stack:** Python 3 / FastAPI, Redis (existing `db/redis` domain package), hermetic standalone tests (in-memory fake Redis, `check()` harness, `sys.exit`), registered in `.github/workflows/ci.yml`.

### Phase 1 roadmap (this plan is 1 of 3)
- **Plan 1 (this doc):** backend accounts + signup + demo + native login. Working/testable alone.
- **Plan 2:** Login with RentOk (Firebase → RentOk `check-email`) + Activate (attach real `pg_ids`, demo→live). Builds on Plan 1's account store.
- **Plan 3:** Admin-panel onboarding UI (React/TS): signup screen, dual-login screen, activate wizard. Consumes Plans 1–2.

### Notes carried from the design spec
- Spec: `docs/superpowers/specs/2026-06-05-brand-self-serve-signup-design.md`.
- **The admin panel is React 19 + TypeScript + Vite** (`src/lib/`, `src/pages/`, `App.tsx`, Vercel edge proxies in `api/`). The backend already has `POST /admin/login` → `core/admin_login.py:verify_admin_login` (a *single* env-configured credential returning one brand key). Plan 1 generalizes that to one record per brand. The `claude-booking-bot/CLAUDE.md` admin-panel section is **stale** (describes the old vanilla-JS panel) — out of scope to fix here; flagged separately.
- **Demo only returns live properties if `DEMO_PG_IDS` is set to real sandbox pg_ids.** That's an env/ops value (default empty), not code.

---

## File Structure

**Create:**
- `claude-booking-bot/db/redis/accounts.py` — account + email-verify-token Redis CRUD.
- `claude-booking-bot/core/demo_brand.py` — pure demo `brand_config` builder + provisioner.
- `claude-booking-bot/core/accounts.py` — signup / native login / email-verify orchestration + password hashing + validation.
- `claude-booking-bot/test_accounts_signup.py` — hermetic regression (built up across tasks).

**Modify:**
- `claude-booking-bot/config.py` — add `DEMO_PG_IDS`, `DEMO_CITIES`, `DEMO_AREAS`, `ADMIN_BASE_URL`.
- `claude-booking-bot/routers/admin.py` — add `POST /admin/signup`, `POST /admin/verify-email`; extend `POST /admin/login` to try the account store before the legacy env credential.
- `claude-booking-bot/db/redis/__init__.py` and `claude-booking-bot/db/redis_store.py` — re-export the new account functions (repo convention; explicit import lists, NOT `import *`).
- `claude-booking-bot/.github/workflows/ci.yml` — add `test_accounts_signup.py` to the hermetic gate.

All commands below assume CWD `claude-booking-bot/`.

---

## Task 1: Config fields for the demo brand + admin base URL

**Files:**
- Modify: `config.py` (add fields to the existing `Settings` class)

- [ ] **Step 1: Add the fields**

Add these inside the `Settings` class in `config.py`, next to the other feature/brand fields:

```python
    # Self-serve signup — demo brand provisioning
    DEMO_PG_IDS: list[str] = []        # sandbox property ids a fresh signup's demo bot serves (set in env for live demo)
    DEMO_CITIES: list[str] = ["Mumbai"]
    DEMO_AREAS: list[str] = []
    ADMIN_BASE_URL: str = "https://eazypg-admin.vercel.app"  # used to build the email-verification link
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "from config import settings; print(settings.DEMO_PG_IDS, settings.ADMIN_BASE_URL)"`
Expected: `[] https://eazypg-admin.vercel.app`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(signup): add demo-brand + admin-base-url settings"
```

---

## Task 2: Account store (`db/redis/accounts.py`) + test harness

**Files:**
- Create: `db/redis/accounts.py`
- Create: `test_accounts_signup.py`
- Modify: `db/redis/__init__.py`, `db/redis_store.py`

- [ ] **Step 1: Write the failing test (store CRUD + verify token)**

Create `test_accounts_signup.py`:

```python
"""Hermetic regression for self-serve brand accounts (no Redis/network/LLM)."""
import os, sys
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

# ---- in-memory fake Redis -------------------------------------------------
import db.redis._base as _base

class _FakePipe:
    def __init__(self, store): self.store = store; self._ops = []
    def set(self, k, v): self._ops.append((k, v)); return self
    def execute(self):
        for k, v in self._ops:
            self.store[k] = v.encode() if isinstance(v, str) else v
        self._ops = []

class _FakeRedis:
    def __init__(self): self.store = {}
    def get(self, k): return self.store.get(k)
    def set(self, k, v): self.store[k] = v.encode() if isinstance(v, str) else v
    def setex(self, k, ttl, v): self.store[k] = v.encode() if isinstance(v, str) else v
    def delete(self, k): self.store.pop(k, None)
    def exists(self, k): return 1 if k in self.store else 0
    def pipeline(self): return _FakePipe(self.store)

_fake = _FakeRedis()
_base._r = lambda: _fake

# Rebind _r in the domain modules that imported it by value.
import db.redis.brand as _brand
import db.redis.accounts as _accounts
_brand._r = lambda: _fake
_accounts._r = lambda: _fake

# ---- tiny harness ---------------------------------------------------------
_passed = 0; _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1; print(f"  PASS {name}")
    else: _failed += 1; print(f"  FAIL {name}")

# ---- store tests ----------------------------------------------------------
from db.redis.accounts import (
    get_account, save_account, account_exists,
    set_email_verify_token, consume_email_verify_token,
)

def test_store():
    print("test_store")
    check("absent account is None", get_account("nobody@example.com") is None)
    save_account({"email": "a@b.com", "password_sha256": "x", "api_key": "eapg_k", "brand_hash": "h", "email_verified": False})
    check("account round-trips", get_account("a@b.com")["api_key"] == "eapg_k")
    check("email lookup is case-insensitive", get_account("A@B.COM")["api_key"] == "eapg_k")
    check("account_exists true", account_exists("a@b.com") is True)
    check("account_exists false", account_exists("z@z.com") is False)
    set_email_verify_token("tok123", "a@b.com")
    check("token resolves to email", consume_email_verify_token("tok123") == "a@b.com")
    check("token is single-use", consume_email_verify_token("tok123") is None)
    check("unknown token is None", consume_email_verify_token("nope") is None)

if __name__ == "__main__":
    test_store()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python test_accounts_signup.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'db.redis.accounts'`

- [ ] **Step 3: Implement `db/redis/accounts.py`**

Create `db/redis/accounts.py`:

```python
"""
db/redis/accounts.py — Self-serve brand accounts + email-verification tokens.

Redis keys:
  brand_account:{email_lower}   — account JSON {email, password_sha256, api_key, brand_hash, email_verified, created_at} (no TTL)
  email_verify:{token}          — token → email_lower (24h TTL, single-use)

The account record deliberately holds the brand's own api_key so /admin/login can
return it (the panel sends it as X-API-Key). This generalizes the single
ADMIN_LOGIN_API_KEY model to one record per brand. Raw keys of OTHER brands are
never stored here — each record holds only its own.
"""

from db.redis._base import _r, _json_get, _json_set

EMAIL_VERIFY_TTL = 86400  # 24h


def _account_key(email: str) -> str:
    return f"brand_account:{email.strip().lower()}"


def get_account(email: str):
    """Return the account dict for an email (case-insensitive), or None."""
    return _json_get(_account_key(email))


def save_account(account: dict) -> None:
    """Persist an account record (keyed by lowercased email)."""
    _json_set(_account_key(account["email"]), account)


def account_exists(email: str) -> bool:
    return get_account(email) is not None


def set_email_verify_token(token: str, email: str) -> None:
    """Store token → email with a 24h TTL."""
    _json_set(f"email_verify:{token}", email.strip().lower(), ex=EMAIL_VERIFY_TTL)


def consume_email_verify_token(token: str):
    """Return the email for a verification token and delete it (single-use). None if missing/expired."""
    email = _json_get(f"email_verify:{token}")
    if email is not None:
        _r().delete(f"email_verify:{token}")
    return email
```

- [ ] **Step 4: Re-export (repo convention)**

In `db/redis/__init__.py`, add to the import/re-export block:

```python
from db.redis.accounts import (
    get_account, save_account, account_exists,
    set_email_verify_token, consume_email_verify_token,
)
```

In `db/redis_store.py`, add the same names to its explicit re-export import list (find the existing `from db.redis import (...)` block and add the five names).

- [ ] **Step 5: Run test to verify it passes**

Run: `python test_accounts_signup.py`
Expected: PASS — `8 passed, 0 failed`

- [ ] **Step 6: Commit**

```bash
git add db/redis/accounts.py db/redis/__init__.py db/redis_store.py test_accounts_signup.py
git commit -m "feat(signup): brand account + email-verify-token Redis store"
```

---

## Task 3: Demo brand provisioner (`core/demo_brand.py`)

**Files:**
- Create: `core/demo_brand.py`
- Modify: `test_accounts_signup.py`

- [ ] **Step 1: Add the failing test**

Add to `test_accounts_signup.py` (above the `__main__` block):

```python
from config import settings
from core.demo_brand import build_demo_config, provision_demo_brand
from db.redis.brand import get_brand_config, _brand_hash

def test_demo_brand():
    print("test_demo_brand")
    settings.DEMO_PG_IDS = ["pg_demo_1", "pg_demo_2"]
    settings.DEMO_CITIES = ["Mumbai"]
    settings.DEMO_AREAS = ["Kurla"]
    cfg = build_demo_config("Acme PG")
    check("demo brand_name", cfg["brand_name"] == "Acme PG")
    check("demo pg_ids copied", cfg["pg_ids"] == ["pg_demo_1", "pg_demo_2"])
    check("demo is_demo flag", cfg["is_demo"] is True)
    check("demo status", cfg["status"] == "demo")
    check("demo has link token", bool(cfg["brand_link_token"]))
    check("blank name → default", build_demo_config("")["brand_name"] == "My Demo PG")

    api_key = "eapg_demotest"
    provisioned = provision_demo_brand(api_key, "Acme PG")
    stored = get_brand_config(api_key)
    check("provision persists brand_config", stored is not None)
    check("provision sets brand_hash", stored["brand_hash"] == _brand_hash(api_key))
    check("provision keeps pg_ids", stored["pg_ids"] == ["pg_demo_1", "pg_demo_2"])
    check("provision returns same token", provisioned["brand_link_token"] == stored["brand_link_token"])
```

And call it in `__main__`: add `test_demo_brand()` after `test_store()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python test_accounts_signup.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.demo_brand'`

- [ ] **Step 3: Implement `core/demo_brand.py`**

Create `core/demo_brand.py`:

```python
"""
core/demo_brand.py — Provision a starter DEMO brand for a fresh signup.

A new account gets an immediately-working web bot on sandbox inventory: a
brand_config pointing at settings.DEMO_PG_IDS, marked is_demo=True / status="demo".
Going live (Plan 2 Activate) swaps the demo pg_ids for the brand's real ones.
"""

import time
import uuid

from config import settings
from db.redis.brand import set_brand_config


def build_demo_config(brand_name: str) -> dict:
    """Pure builder for a demo brand_config dict (no Redis writes)."""
    now = int(time.time())
    return {
        "brand_name": (brand_name or "").strip() or "My Demo PG",
        "pg_ids": list(settings.DEMO_PG_IDS),
        "cities": list(settings.DEMO_CITIES),
        "areas": list(settings.DEMO_AREAS),
        "brand_link_token": str(uuid.uuid4()),
        "is_demo": True,
        "status": "demo",
        "created_at": now,
        "updated_at": now,
    }


def provision_demo_brand(api_key: str, brand_name: str) -> dict:
    """Create + persist the demo brand_config for a new account's api_key. Returns the config."""
    config = build_demo_config(brand_name)
    set_brand_config(api_key, config)  # also writes brand_token + injects brand_hash
    return config
```

- [ ] **Step 4: Run to verify it passes**

Run: `python test_accounts_signup.py`
Expected: PASS — all `test_store` + `test_demo_brand` checks pass, `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add core/demo_brand.py test_accounts_signup.py
git commit -m "feat(signup): demo brand provisioner on sandbox inventory"
```

---

## Task 4: Signup orchestration (`core/accounts.py` — signup)

**Files:**
- Create: `core/accounts.py`
- Modify: `test_accounts_signup.py`

- [ ] **Step 1: Add the failing test**

Add to `test_accounts_signup.py`:

```python
import hashlib
from core.accounts import signup, validate_signup

def _sha(s): return hashlib.sha256(s.encode()).hexdigest()

def test_signup():
    print("test_signup")
    settings.DEMO_PG_IDS = ["pg_demo_1"]
    check("bad email rejected", validate_signup("nope", "password123") is not None)
    check("short password rejected", validate_signup("a@b.com", "short") is not None)
    check("valid input ok", validate_signup("a@b.com", "password123") is None)

    result, reason = signup("New@Brand.com", "password123", "Acme PG")
    check("signup ok", reason == "ok")
    check("api_key prefix", result["api_key"].startswith("eapg_"))
    check("returns link token", bool(result["brand_link_token"]))

    acct = get_account("new@brand.com")
    check("account stored (lowercased)", acct is not None)
    check("password hashed not plaintext", acct["password_sha256"] == _sha("password123"))
    check("no plaintext password stored", "password" not in acct)
    check("email starts unverified", acct["email_verified"] is False)

    brand = get_brand_config(result["api_key"])
    check("demo brand provisioned", brand is not None and brand["is_demo"] is True)
    check("verify token issued", bool(result["verify_token"]))

    _, dup_reason = signup("new@brand.com", "password123", "Dup")
    check("duplicate email rejected", dup_reason == "exists")

    _, bad_reason = signup("bademail", "password123", "X")
    check("invalid signup reason", bad_reason.startswith("invalid:"))
```

Call `test_signup()` in `__main__` after `test_demo_brand()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python test_accounts_signup.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.accounts'`

- [ ] **Step 3: Implement `core/accounts.py` (signup half)**

Create `core/accounts.py`:

```python
"""
core/accounts.py — Self-serve brand signup + native email/password login.

Signup: validate → ensure the email is free → generate a unique brand api_key →
provision a demo brand → store the account (password sha256'd) → issue an email
verification token. Login: look up the account, constant-time password check,
return the brand api_key the panel sends as X-API-Key.
"""

import hashlib
import hmac
import re
import secrets
import time

from core.log import get_logger
from core.demo_brand import provision_demo_brand
from db.redis.accounts import (
    account_exists, get_account, save_account,
    set_email_verify_token, consume_email_verify_token,
)
from db.redis.brand import _brand_hash, get_brand_config

logger = get_logger("accounts")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _generate_api_key() -> str:
    """Random, unguessable brand key the panel sends as X-API-Key."""
    return "eapg_" + secrets.token_urlsafe(24)


def validate_signup(email: str, password: str) -> str | None:
    """Return an error string on invalid input, or None if OK."""
    if not email or not _EMAIL_RE.match(email.strip()):
        return "Enter a valid email address."
    if not password or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    return None


def signup(email: str, password: str, brand_name: str = "") -> tuple[dict | None, str]:
    """Create an account + demo brand. Returns (result, reason).

    result on success: {api_key, brand_hash, brand_link_token, verify_token, email}.
    reason: "ok" | "invalid:<msg>" | "exists".
    """
    err = validate_signup(email, password)
    if err:
        return None, f"invalid:{err}"
    email_norm = email.strip().lower()
    if account_exists(email_norm):
        return None, "exists"

    api_key = _generate_api_key()
    demo = provision_demo_brand(api_key, brand_name)  # persists brand_config first
    brand_hash = _brand_hash(api_key)

    save_account({
        "email": email_norm,
        "password_sha256": _sha256_hex(password),
        "api_key": api_key,
        "brand_hash": brand_hash,
        "email_verified": False,
        "created_at": int(time.time()),
    })

    verify_token = secrets.token_urlsafe(24)
    set_email_verify_token(verify_token, email_norm)
    logger.info("Self-serve signup: %s (brand_hash=%s)", email_norm, brand_hash)

    return {
        "api_key": api_key,
        "brand_hash": brand_hash,
        "brand_link_token": demo["brand_link_token"],
        "verify_token": verify_token,
        "email": email_norm,
    }, "ok"
```

- [ ] **Step 4: Run to verify it passes**

Run: `python test_accounts_signup.py`
Expected: PASS — `test_signup` checks all pass, `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add core/accounts.py test_accounts_signup.py
git commit -m "feat(signup): account signup orchestration + demo provisioning"
```

---

## Task 5: Native login + email verification (`core/accounts.py`)

**Files:**
- Modify: `core/accounts.py`, `test_accounts_signup.py`

- [ ] **Step 1: Add the failing test**

Add to `test_accounts_signup.py`:

```python
from core.accounts import verify_login, verify_email

def test_login_and_verify():
    print("test_login_and_verify")
    settings.DEMO_PG_IDS = ["pg_demo_1"]
    result, reason = signup("login@brand.com", "password123", "Acme")
    assert reason == "ok"

    key, lr = verify_login("login@brand.com", "password123")
    check("correct login ok", lr == "ok")
    check("login returns the brand key", key == result["api_key"])
    check("returned key resolves to brand", get_brand_config(key) is not None)

    check("case-insensitive login", verify_login("LOGIN@BRAND.COM", "password123")[1] == "ok")
    check("wrong password rejected", verify_login("login@brand.com", "WRONG")[1] == "invalid")
    check("unknown user rejected", verify_login("ghost@x.com", "password123")[1] == "invalid")

    # email verification
    check("starts unverified", get_account("login@brand.com")["email_verified"] is False)
    check("verify ok", verify_email(result["verify_token"]) is True)
    check("now verified", get_account("login@brand.com")["email_verified"] is True)
    check("token single-use", verify_email(result["verify_token"]) is False)
    check("bad token rejected", verify_email("garbage") is False)
```

Call `test_login_and_verify()` in `__main__` after `test_signup()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python test_accounts_signup.py`
Expected: FAIL — `ImportError: cannot import name 'verify_login'`

- [ ] **Step 3: Implement login + verify (append to `core/accounts.py`)**

Append to `core/accounts.py`:

```python
def verify_login(email: str, password: str) -> tuple[str | None, str]:
    """Native email/password login. Returns (api_key, reason).

    reason: "ok" | "invalid" | "misconfigured".
    """
    account = get_account(email)
    if not account:
        return None, "invalid"
    if not hmac.compare_digest(_sha256_hex(password or ""), account.get("password_sha256", "")):
        return None, "invalid"
    api_key = account.get("api_key", "")
    if not get_brand_config(api_key):
        logger.error("Account %s has no brand config — provisioning drift", email)
        return None, "misconfigured"
    return api_key, "ok"


def verify_email(token: str) -> bool:
    """Consume a verification token and flip the account's email_verified. False if bad/expired."""
    email = consume_email_verify_token(token)
    if not email:
        return False
    account = get_account(email)
    if not account:
        return False
    account["email_verified"] = True
    save_account(account)
    return True


def send_verification_email(email: str, token: str) -> None:
    """Pluggable delivery. Defaults to LOGGING the link — no email provider wired in v1.

    Swap this body for a real provider (Resend/SES/etc.) when delivery is needed.
    """
    from config import settings
    link = f"{settings.ADMIN_BASE_URL}/verify-email?token={token}"
    logger.info("[email-verify] %s -> %s", email, link)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python test_accounts_signup.py`
Expected: PASS — all four test groups pass, `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add core/accounts.py test_accounts_signup.py
git commit -m "feat(signup): native login + email verification"
```

---

## Task 6: Wire endpoints (`routers/admin.py`)

**Files:**
- Modify: `routers/admin.py` (add 2 routes; extend `/admin/login`)
- Modify: `test_accounts_signup.py` (login-precedence test)

- [ ] **Step 1: Add the failing test (login precedence: account store before legacy)**

Add to `test_accounts_signup.py`:

```python
def test_login_precedence():
    print("test_login_precedence")
    # An account login should win without consulting the legacy env credential.
    import core.accounts as acc
    import core.admin_login as legacy
    settings.DEMO_PG_IDS = ["pg_demo_1"]
    res, _ = signup("prec@brand.com", "password123", "Acme")

    legacy_called = {"n": 0}
    orig = legacy.verify_admin_login
    legacy.verify_admin_login = lambda u, p: (legacy_called.__setitem__("n", legacy_called["n"] + 1) or (None, "invalid"))
    try:
        # Simulate the endpoint's resolution order.
        key, reason = acc.verify_login("prec@brand.com", "password123")
        if reason != "ok":
            key, reason = legacy.verify_admin_login("prec@brand.com", "password123")
        check("account login wins", reason == "ok" and key == res["api_key"])
        check("legacy not consulted on account hit", legacy_called["n"] == 0)
    finally:
        legacy.verify_admin_login = orig
```

Call `test_login_precedence()` in `__main__` after `test_login_and_verify()`.

- [ ] **Step 2: Run to verify it passes the new unit (logic lives in test) — then add routes**

Run: `python test_accounts_signup.py`
Expected: PASS (this test asserts the resolution order we implement in the route next).

- [ ] **Step 3: Add the two routes + extend login in `routers/admin.py`**

Find the existing `@router.post("/admin/login")` handler (around line 858). Replace its body to consult the account store first, and add the two new routes immediately after it:

```python
@router.post("/admin/login")
async def admin_login(request: Request):
    from core.admin_login import verify_admin_login
    from core.accounts import verify_login
    body = await request.json()
    username = body.get("username") or ""
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")

    # Self-serve accounts first (username == email); fall back to the legacy env credential.
    api_key, reason = verify_login(username, password)
    if reason != "ok":
        api_key, reason = verify_admin_login(username, password)

    if reason == "ok":
        return {"status": 200, "api_key": api_key}
    if reason == "throttled":
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    if reason in ("unconfigured", "misconfigured"):
        raise HTTPException(status_code=503, detail="Login is not configured")
    raise HTTPException(status_code=401, detail="Invalid username or password")


@router.post("/admin/signup")
async def admin_signup(request: Request):
    from core.accounts import signup, send_verification_email
    body = await request.json()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    brand_name = (body.get("brand_name") or "").strip()

    result, reason = signup(email, password, brand_name)
    if reason == "ok":
        send_verification_email(result["email"], result["verify_token"])
        # Return the api_key so the panel logs straight into the demo.
        return {
            "status": 200,
            "api_key": result["api_key"],
            "brand_link_token": result["brand_link_token"],
            "email_verified": False,
        }
    if reason == "exists":
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    if reason.startswith("invalid:"):
        raise HTTPException(status_code=400, detail=reason.split("invalid:", 1)[1])
    raise HTTPException(status_code=400, detail="Signup failed.")


@router.post("/admin/verify-email")
async def admin_verify_email(request: Request):
    from core.accounts import verify_email
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    if verify_email(token):
        return {"status": 200, "verified": True}
    raise HTTPException(status_code=400, detail="Invalid or expired verification link.")
```

(`Request` and `HTTPException` are already imported in `routers/admin.py` — confirm at the top of the file; the existing `/admin/login` uses both.)

- [ ] **Step 4: Verify the app imports cleanly**

Run: `python -c "import routers.admin"`
Expected: no error (module imports).

- [ ] **Step 5: Run the full hermetic test**

Run: `python test_accounts_signup.py`
Expected: PASS — all groups, `0 failed`.

- [ ] **Step 6: Commit**

```bash
git add routers/admin.py test_accounts_signup.py
git commit -m "feat(signup): /admin/signup + /admin/verify-email + account-first login"
```

---

## Task 7: Register in CI gate + final green

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the test to the hermetic suite**

In `.github/workflows/ci.yml`, find the list of test runs (e.g. the `python test_shortlist_contract.py` line) and add alongside them:

```yaml
      - run: python test_accounts_signup.py
```

- [ ] **Step 2: Run the test once more locally**

Run: `python test_accounts_signup.py`
Expected: PASS — `0 failed`.

- [ ] **Step 3: Sanity-check the rest of the hermetic gate still passes**

Run: `python test_tenant_isolation.py && python test_shortlist_contract.py`
Expected: both exit 0 (no regression from the re-export / login edits).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci(signup): add test_accounts_signup to hermetic gate"
```

---

## Self-Review (run before handoff)

**Spec coverage (Plan 1 slice):**
- Public signup (email + password) → ✅ Task 4/6 (`/admin/signup`).
- Instant demo brand on sandbox inventory → ✅ Task 3 (`provision_demo_brand`) + returned `brand_link_token`.
- Native login → ✅ Task 5/6 (`verify_login`, account-first `/admin/login`).
- Email verification issued at signup, gates go-live later → ✅ Task 5 (`verify_email`, `/admin/verify-email`); delivery deferred to a pluggable logger.
- Chicken-and-egg solved → ✅ signup is public and *creates* the brand; everything else stays behind `require_admin_brand_key`.
- Out of Plan 1 (correctly deferred): Login with RentOk, Activate/attach real pg_ids (Plan 2); React UI (Plan 3); real email delivery (config swap).

**Placeholder scan:** none — every step has runnable code/commands.

**Type/name consistency:** `signup`→`(result, reason)`; `verify_login`→`(api_key, reason)`; `verify_email`→`bool`; account dict keys (`email`, `password_sha256`, `api_key`, `brand_hash`, `email_verified`, `created_at`) consistent across store, signup, login, tests; `_brand_hash`/`get_brand_config`/`set_brand_config` used exactly as defined in `db/redis/brand.py`.

**Operational note (not code):** set `DEMO_PG_IDS` (+ `DEMO_CITIES`/`DEMO_AREAS`) in the backend env to real sandbox pg_ids so a fresh signup's demo bot returns live properties; default empty = empty demo.

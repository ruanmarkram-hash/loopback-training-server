import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth_events import client_ip, record_auth_event
from app.database import DbSession, SessionLocal
from app.models.api_token import ApiToken
from app.models.user import User
from app.security import hash_token

security = HTTPBearer()

# Don't write last_used_at on every request — only if it's this stale.
_TOUCH_INTERVAL = timedelta(minutes=5)
_ADMIN_WRITE_LOCK_KEY = 0x4C624164  # "LbAd": cross-account admin mutations, separate from setup.

# A stranded device (revoked/expired token, deactivated account) retries in
# the background, so one dead token can 401 every few minutes for days. Audit
# at most one token_rejected event per key per window — the key is the token
# for expired/inactive (one stranded device = one clean signal) and the IP for
# unknown tokens (a scanner rotating garbage tokens can't mint a row per
# attempt). In-process state, same trade-off as the login rate-limit throttle
# in main.py.
_REJECT_EVENT_WINDOW_S = 6 * 3600.0
_reject_event_last: dict[str, float] = {}


def _record_rejection(
    db: Session,
    request: Request,
    *,
    reason: str,
    key: str,
    username: str | None = None,
    user_id: Any = None,
    detail: dict[str, Any] | None = None,
) -> None:
    now = time.monotonic()
    last = _reject_event_last.get(key)
    if last is not None and now - last < _REJECT_EVENT_WINDOW_S:
        return
    if len(_reject_event_last) > 1024:  # bound the map under a many-key flood
        _reject_event_last.clear()
    _reject_event_last[key] = now
    # commit=True: the 401 path writes nothing else (same as login_failed).
    record_auth_event(
        db,
        "token_rejected",
        username=username,
        user_id=user_id,
        ip=client_ip(request),
        detail={"reason": reason, **(detail or {})},
        commit=True,
    )


def _touch_last_used(
    token_id,
    last_used_at: datetime | None,
    now: datetime,
    user_agent: str | None,
    prev_user_agent: str | None,
) -> None:
    # A changed User-Agent (app updated, different client) always writes —
    # that transition is the version-handshake signal and it's rare.
    if last_used_at is not None and now - last_used_at < _TOUCH_INTERVAL and user_agent == prev_user_agent:
        return
    # Separate session so this bookkeeping write never entangles with the
    # request's own transaction (which may commit or roll back independently).
    with SessionLocal() as s:
        s.execute(update(ApiToken).where(ApiToken.id == token_id).values(last_used_at=now, last_user_agent=user_agent))
        s.commit()


def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
) -> User:
    token_hash = hash_token(credentials.credentials)
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash))
    if token is None:
        # Revoked or never existed — no row to attribute. The hash suffix lets
        # the feed tell one dead token repeating from many different ones.
        _record_rejection(
            db,
            request,
            reason="unknown",
            key=f"ip:{client_ip(request)}",
            detail={"token_hint": token_hash[-6:]},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked token")

    now = datetime.now(UTC)
    if token.expires_at is not None and token.expires_at < now:
        _record_rejection(
            db,
            request,
            reason="expired",
            key=f"token:{token.id}",
            username=token.user.username,
            user_id=token.user_id,
            detail={"name": token.name},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")

    user = token.user
    if not user.is_active:
        _record_rejection(
            db,
            request,
            reason="inactive",
            key=f"token:{token.id}",
            username=user.username,
            user_id=user.id,
            detail={"name": token.name},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User is inactive")

    request.state.token_scope = token.scope
    request.state.token_id = token.id
    if token.scope == "coach_worker":
        allowed = request.method == "POST" and (
            request.scope["path"] == "/api/coaching/worker/claim"
            or re.fullmatch(r"/api/coaching/worker/jobs/[0-9a-f-]{36}/result", request.scope["path"])
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="Worker token is proposal-only")
    elif token.scope != "device":
        raise HTTPException(status_code=403, detail="Unsupported token scope")

    ua = (request.headers.get("user-agent") or "").strip()[:300] or None
    _touch_last_used(token.id, token.last_used_at, now, ua, token.last_user_agent)
    # Serialize mutations before endpoints read plan/queue state, not at flush.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        # Admin endpoints may update another account and reference both actor
        # and target in audit rows. Serialize this class before locking either
        # account; otherwise two admins can hold crossed requester/target locks.
        lock_ids = {user.id}
        if request.scope["path"].startswith("/api/admin/"):
            db.execute(select(func.pg_advisory_xact_lock(_ADMIN_WRITE_LOCK_KEY)))
            target = re.match(r"/api/admin/users/([0-9a-fA-F-]{36})(?:/|$)", request.scope["path"])
            if target:
                try:
                    lock_ids.add(uuid.UUID(target.group(1)))
                except ValueError:
                    pass  # The typed route rejects malformed IDs without a write.
        # Target locking also serializes deactivation/token revocation against
        # athlete-owned mutations before either route reads mutable state.
        locked = db.scalars(
            select(User)
            .where(User.id.in_(lock_ids))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        user = next((row for row in locked if row.id == user.id), None)
        current_token = db.scalar(
            select(ApiToken).where(ApiToken.id == token.id).execution_options(populate_existing=True)
        )
        # Authentication was read before a potentially long lock wait. Retain
        # immediate revocation, active-account and fresh admin-role semantics.
        if current_token is None or user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid or revoked token")
        if current_token.expires_at is not None and current_token.expires_at < datetime.now(UTC):
            raise HTTPException(status_code=401, detail="Token expired")
        if current_token.scope != request.state.token_scope:
            raise HTTPException(status_code=403, detail="Token scope changed")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]

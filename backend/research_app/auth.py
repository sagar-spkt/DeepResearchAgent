import hashlib
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import User

bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=8)
def key_client(issuer):
    return jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_jwk_set=True, lifespan=300)


def current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
):
    settings = request.app.state.settings
    if settings.environment == "local":
        # Explicit local-only identity simulator; never available in AWS environments.
        email = request.headers.get("x-dev-user", "").strip().lower()
        if not email or len(email) > 320 or "@" not in email:
            raise HTTPException(401, "Set X-Dev-User for local development")
        subject = "local-" + hashlib.sha256(email.encode()).hexdigest()
        owner = email == settings.bootstrap_owner_email.lower()
    else:
        if credentials is None:
            raise HTTPException(401, "Bearer access token required")
        try:
            token = credentials.credentials
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise ValueError("Invalid token algorithm")
            signing_key = key_client(settings.cognito_issuer).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=settings.cognito_issuer,
                options={
                    "verify_aud": False,
                    "require": ["exp", "iat", "sub", "token_use", "client_id"],
                },
            )
            if claims["token_use"] != "access" or claims["client_id"] != settings.cognito_client_id:
                raise ValueError("Invalid access token audience")
            subject = claims["sub"]
            # Cognito access tokens generally omit email. Never trust a browser email header.
            email = str(claims.get("email", subject))
            owner = False  # Cloud owner creation is an explicit administrative command.
        except Exception as exc:
            raise HTTPException(401, "Invalid or unavailable identity verification") from exc
    factory = request.app.state.session_factory
    with factory() as session:
        user = session.get(User, subject)
        if user is None:
            user = User(id=subject, email=email, role="owner" if owner else "user", approved=owner)
            session.add(user)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                user = session.scalar(select(User).where(User.id == subject))
        return user


def approved_user(user: User = Depends(current_user)):
    if not user.approved:
        raise HTTPException(403, "Administrator approval is required for research")
    return user


def verified_profile_email(id_token, user, settings):
    """Bind display identity to the authenticated subject; never import token roles."""
    try:
        if jwt.get_unverified_header(id_token).get("alg") != "RS256":
            raise ValueError("Invalid ID-token algorithm")
        signing = key_client(settings.cognito_issuer).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing.key,
            algorithms=["RS256"],
            issuer=settings.cognito_issuer,
            audience=settings.cognito_client_id,
            options={
                "require": ["exp", "iat", "sub", "aud", "token_use", "email", "email_verified"]
            },
        )
        email = claims["email"]
        if (
            claims["token_use"] != "id"
            or claims["sub"] != user.id
            or claims["aud"] != settings.cognito_client_id
            or claims["email_verified"] is not True
        ):
            raise ValueError("ID-token identity binding mismatch")
        if not isinstance(email, str) or "@" not in email or len(email) > 320:
            raise ValueError("Invalid verified email")
        return email.strip().lower()
    except Exception as exc:
        raise HTTPException(401, "Invalid or unavailable verified identity") from exc


def admin_user(user: User = Depends(current_user)):
    if not user.approved or user.role not in {"owner", "admin"}:
        raise HTTPException(403, "Administrator access required")
    return user

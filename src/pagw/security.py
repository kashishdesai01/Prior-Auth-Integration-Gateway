import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
from cryptography.fernet import Fernet
from jwt import PyJWK

from pagw.config import Settings
from pagw.http import bounded_request

CLIENTS = {
    "provider-a": ("tenant-a", {"submit", "read", "webhooks"}),
    "provider-b": ("tenant-b", {"submit", "read", "webhooks"}),
    "operator": ("tenant-a", {"submit", "read", "webhooks", "ops"}),
}


@dataclass(frozen=True)
class Principal:
    client_id: str
    tenant: str
    scopes: frozenset[str]


class AuthenticationError(ValueError):
    pass


def signature(secret: str, body: bytes, timestamp: int) -> str:
    return hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()


def callback_key(settings: Settings, payer: str) -> str:
    return hmac.new(
        settings.callback_secret.get_secret_value().encode(), payer.encode(), hashlib.sha256
    ).hexdigest()


def verify_signature(secret: str, body: bytes, timestamp: str, value: str) -> None:
    try:
        parsed = int(timestamp)
        if abs(time.time() - parsed) > 300 or not hmac.compare_digest(
            value, "v1=" + signature(secret, body, parsed)
        ):
            raise AuthenticationError("invalid-signature")
    except (ValueError, TypeError) as exc:
        raise AuthenticationError("invalid-signature") from exc


class TokenVerifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.keys: dict = {}
        self.refreshed = 0.0

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise AuthenticationError("invalid-token")
            with self.lock:
                # Bound unknown-kid refreshes; do not let attackers force one fetch per request.
                elapsed = time.monotonic() - self.refreshed
                if (
                    not self.keys
                    or elapsed > 300
                    or (header["kid"] not in self.keys and elapsed > 5)
                ):
                    with httpx.Client(timeout=3, trust_env=False) as client:
                        response = bounded_request(
                            client, "GET", self.settings.jwks_url, limit=65536, seconds=3
                        )
                        response.raise_for_status()
                        if len(response.content) > 65536:
                            raise AuthenticationError("jwks-too-large")
                        self.keys = {
                            x["kid"]: PyJWK.from_dict(x).key for x in response.json()["keys"]
                        }
                    self.refreshed = time.monotonic()
                key = self.keys.get(header["kid"])
            if key is None:
                raise AuthenticationError("invalid-token")
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
            if self.settings.client_registry:
                registration = self.settings.client_registry[claims["sub"]]
                tenant, allowed = registration["tenant"], set(registration["scopes"])
            else:
                tenant, allowed = CLIENTS[claims["sub"]]
            if not isinstance(claims.get("scope", ""), str):
                raise AuthenticationError("invalid-token-scope")
            scopes = frozenset(claims.get("scope", "").split()) & allowed
            return Principal(claims["sub"], tenant, scopes)
        except (jwt.PyJWTError, KeyError, TypeError, httpx.HTTPError, ValueError) as exc:
            raise AuthenticationError("invalid-token") from exc


def client_assertion(settings: Settings, client_id: str, audience: str) -> str:
    return jwt.encode(
        {
            "iss": client_id,
            "sub": client_id,
            "aud": audience,
            "iat": int(time.time()),
            "exp": int(time.time()) + 120,
            "jti": str(uuid4()),
        },
        settings.client_private_key.get_secret_value()
        if settings.client_private_key
        else Path(settings.client_key_path).read_bytes(),
        algorithm="RS384",
        headers={"kid": "demo-client"},
    )


class TokenManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[str, float]] = {}

    def get(self, payer: str, force=False) -> str:
        with self.lock:
            token, expires = self.cache.get(payer, ("", 0))
            if not force and time.time() < expires - 30:
                return token
            url = f"{self.settings.payer_base_url}/{payer}/oauth/token"
            data = {"grant_type": "client_credentials"}
            auth = None
            if payer == "alpha":
                auth = ("gateway", self.settings.payer_client_secret.get_secret_value())
            else:
                data.update(
                    client_id="gateway",
                    client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    client_assertion=client_assertion(self.settings, "gateway", url),
                )
            with httpx.Client(
                timeout=self.settings.http_timeout_seconds, trust_env=False
            ) as client:
                response = bounded_request(
                    client,
                    "POST",
                    url,
                    data=data,
                    auth=auth or httpx.USE_CLIENT_DEFAULT,
                    limit=16384,
                    seconds=self.settings.http_timeout_seconds,
                )
                response.raise_for_status()
                result = response.json()
            self.cache[payer] = (result["access_token"], time.time() + result["expires_in"])
            return result["access_token"]


def cipher(settings: Settings) -> Fernet:
    return Fernet(settings.encryption_key.get_secret_value().encode())


def public_jwk(path: str, kid: str, alg="RS256") -> dict:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise ValueError("RSA key required")
    result = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**result, "kid": kid, "alg": alg, "use": "sig"}

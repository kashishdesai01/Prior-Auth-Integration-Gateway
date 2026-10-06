"""Generate local-only keys and secrets. Never prints their contents."""

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

runtime = Path(".runtime")
runtime.mkdir(mode=0o700, exist_ok=True)
for name in ("idp", "client"):
    path = runtime / f"{name}.pem"
    if not path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            output.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
if not Path(".env").exists():
    values = {
        "PAGW_DATABASE_URL": "postgresql://pagw:local-only@127.0.0.1:55480/pagw_native",
        "PAGW_CALLBACK_SECRET": secrets.token_urlsafe(32),
        "PAGW_WEBHOOK_SECRET": secrets.token_urlsafe(32),
        "PAGW_PAYER_CLIENT_SECRET": secrets.token_urlsafe(32),
        "PAGW_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
    with Path(".env").open("x") as output:
        os.chmod(".env", 0o600)
        output.write("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
print("Local configuration ready; private keys and .env are ignored by Git.")

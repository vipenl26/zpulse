"""zpulse.crypto: symmetric vault encryption, Ed25519 identities, sealed share packets.

- Vault: AES-256-GCM, random 12-byte nonce, AAD = vault_id. Blob = nonce || ct || tag.
- Identity: Ed25519 signing key (pynacl), stored as JSON {"sk": b64, "vk": b64}.
- seal_to/unseal: libsodium SealedBox to the recipient's Ed25519 key converted to X25519.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from nacl.exceptions import BadSignatureError
from nacl.public import SealedBox
from nacl.signing import SigningKey, VerifyKey

VAULT_KEY_LEN = 32
NONCE_LEN = 12


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s, validate=True)


def random_key() -> bytes:
    """A fresh 32-byte vault key."""
    return secrets.token_bytes(VAULT_KEY_LEN)


def encrypt_vault(plaintext: bytes, key: bytes, vault_id: str) -> bytes:
    """AES-256-GCM encrypt; returns nonce || ciphertext || tag."""
    assert len(key) == VAULT_KEY_LEN
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, vault_id.encode())


def decrypt_vault(blob: bytes, key: bytes, vault_id: str) -> bytes:
    """Inverse of encrypt_vault. Raises ValueError on wrong key, wrong vault_id or tampering."""
    if len(key) != VAULT_KEY_LEN or len(blob) < NONCE_LEN + 16:
        raise ValueError("bad key or blob length")
    try:
        return AESGCM(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], vault_id.encode())
    except InvalidTag as e:
        raise ValueError("vault decryption failed (tampered or wrong key)") from e


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Identity:
    """An Ed25519 keypair used for signing memos and receiving sealed share packets."""

    def __init__(self, sk: SigningKey):
        self.sk = sk

    @classmethod
    def generate(cls) -> "Identity":
        return cls(SigningKey.generate())

    @classmethod
    def load(cls, path: str | Path) -> "Identity":
        d = json.loads(Path(path).read_text())
        ident = cls(SigningKey(_unb64(d["sk"])))
        if "vk" in d and d["vk"] != ident.verify_key_b64:
            raise ValueError(f"identity file {path}: vk does not match sk")
        return ident

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"sk": _b64(bytes(self.sk)), "vk": self.verify_key_b64}, f)

    def sign(self, msg: bytes) -> bytes:
        """64-byte detached Ed25519 signature."""
        return self.sk.sign(msg).signature

    @property
    def verify_key_b64(self) -> str:
        return _b64(bytes(self.sk.verify_key))


def verify(vk_b64: str, msg: bytes, sig: bytes) -> bool:
    """True iff sig is a valid Ed25519 signature of msg under vk_b64. Never raises."""
    try:
        VerifyKey(_unb64(vk_b64)).verify(msg, sig)
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def seal_to(vk_b64: str, data: bytes) -> str:
    """Anonymously encrypt data to the holder of vk_b64 (Ed25519 -> X25519). Returns base64."""
    pk = VerifyKey(_unb64(vk_b64)).to_curve25519_public_key()
    return _b64(SealedBox(pk).encrypt(data))


def unseal(identity: Identity, sealed_b64: str) -> bytes:
    """Decrypt a seal_to() blob with our identity. Raises nacl.exceptions.CryptoError on failure."""
    return SealedBox(identity.sk.to_curve25519_private_key()).decrypt(_unb64(sealed_b64))

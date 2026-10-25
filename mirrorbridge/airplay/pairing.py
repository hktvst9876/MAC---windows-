"""AirPlay "legacy" pairing: /pair-setup and /pair-verify.

The receiver has a long-term Ed25519 identity whose public key is advertised
over mDNS ("pk"). During pair-verify both sides exchange ephemeral X25519 keys
and prove possession of their Ed25519 keys with signatures that are encrypted
with AES-128-CTR under a key derived from the X25519 shared secret. That shared
secret is later mixed into the media AES key.
"""

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SALT_KEY = b"Pair-Verify-AES-Key"
SALT_IV = b"Pair-Verify-AES-IV"

_RAW = serialization.Encoding.Raw
_RAW_PUB = serialization.PublicFormat.Raw
_RAW_PRIV = serialization.PrivateFormat.Raw


class PairingError(Exception):
    pass


class PairingIdentity:
    """The receiver's long-term Ed25519 key pair."""

    def __init__(self, private_key: Ed25519PrivateKey | None = None):
        self.private_key = private_key or Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key().public_bytes(_RAW, _RAW_PUB)

    @classmethod
    def from_seed(cls, seed: bytes) -> "PairingIdentity":
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def seed(self) -> bytes:
        return self.private_key.private_bytes(_RAW, _RAW_PRIV, serialization.NoEncryption())


def derive_key(salt: bytes, secret: bytes) -> bytes:
    return hashlib.sha512(salt + secret).digest()[:16]


def _ctr(key: bytes, iv: bytes):
    return Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()


class PairingSession:
    """Pairing state for one RTSP connection."""

    def __init__(self, identity: PairingIdentity):
        self.identity = identity
        self.setup_done = False
        self.verified = False
        self.client_ed_public = None
        self.ecdh_secret = None
        self._ecdh_ours = None
        self._ecdh_ours_public = None
        self._ecdh_theirs = None

    @property
    def started(self) -> bool:
        """True once the client has begun pairing (pair-setup or pair-verify)."""
        return self.setup_done or self.ecdh_secret is not None

    def pair_setup(self, body: bytes) -> bytes:
        if len(body) != 32:
            raise PairingError("invalid pair-setup data")
        self.setup_done = True
        return self.identity.public_key

    def pair_verify(self, body: bytes) -> bytes:
        """Handle one /pair-verify request; returns the response body."""
        if len(body) < 4:
            raise PairingError("invalid pair-verify data")
        if body[0] == 1:
            return self._verify_start(body)
        return self._verify_finish(body)

    def _verify_start(self, body: bytes) -> bytes:
        if len(body) != 4 + 32 + 32:
            raise PairingError("invalid pair-verify data")
        self._ecdh_theirs = body[4:36]
        self.client_ed_public = body[36:68]
        ours = X25519PrivateKey.generate()
        self._ecdh_ours = ours
        self._ecdh_ours_public = ours.public_key().public_bytes(_RAW, _RAW_PUB)
        self.ecdh_secret = ours.exchange(X25519PublicKey.from_public_bytes(self._ecdh_theirs))

        signature = self.identity.private_key.sign(self._ecdh_ours_public + self._ecdh_theirs)
        key = derive_key(SALT_KEY, self.ecdh_secret)
        iv = derive_key(SALT_IV, self.ecdh_secret)
        encrypted = _ctr(key, iv).update(signature)
        return self._ecdh_ours_public + encrypted

    def _verify_finish(self, body: bytes) -> bytes:
        if self.ecdh_secret is None or len(body) != 4 + 64:
            raise PairingError("invalid pair-verify data")
        key = derive_key(SALT_KEY, self.ecdh_secret)
        iv = derive_key(SALT_IV, self.ecdh_secret)
        ctr = _ctr(key, iv)
        # The first 64 bytes of keystream were used for our own signature.
        ctr.update(bytes(64))
        signature = ctr.update(body[4:68])
        try:
            Ed25519PublicKey.from_public_bytes(self.client_ed_public).verify(
                signature, self._ecdh_theirs + self._ecdh_ours_public)
        except InvalidSignature as exc:
            raise PairingError("pair-verify signature mismatch") from exc
        self.verified = True
        return b""

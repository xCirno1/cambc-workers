"""AES-256-GCM file boxes. Everything that leaves a trusted machine goes through seal()."""
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b'CWB1'
MAX_BYTES = 512 * 1024**2  # one-shot AEAD; bundles are a few MB


def seal(src, dst, key, aad=b'cambc-workers/v1'):
    data = Path(src).read_bytes()
    if len(key) != 32 or len(data) > MAX_BYTES:
        raise ValueError('bad box parameters')
    nonce = os.urandom(12)
    Path(dst).write_bytes(MAGIC + nonce + AESGCM(key).encrypt(nonce, data, aad))


def unseal(src, dst, key, aad=b'cambc-workers/v1'):
    raw = Path(src).read_bytes()
    if len(key) != 32 or raw[:4] != MAGIC or len(raw) < 32:
        raise ValueError('bad box')
    Path(dst).write_bytes(AESGCM(key).decrypt(raw[4:16], raw[16:], aad))

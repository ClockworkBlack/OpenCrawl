"""Encrypted files for data that leaves the machine: CI artifacts, release assets, backups.

The crawl database, state and exports can hold personal data (research on named people, screenshots, replies to
records requests). On GitHub, release assets of a public repository and artifacts of its workflow runs can be
downloaded by anyone, so the workflow only ever uploads them encrypted with this module, using a key kept in a
repository secret (OPENCRAWL_DATA_KEY).

Format (version 1): AES-256-GCM in the STREAM construction (Hoang, Reyhanitabar, Rogaway, Vizár), 1 MiB chunks.
  header   "OCVAULT1" | version 1 | kdf (1 = HKDF-SHA256 from a 32-byte key, 2 = scrypt from a passphrase) |
           salt (16) | nonce prefix (7) | chunk size (4, big-endian) | scrypt log2(N), r, p (3; zero for HKDF)
  chunks   each chunk is sealed with nonce = prefix | 32-bit counter | last-chunk flag, and the whole header as
           associated data. Reordering, truncating, extending or editing any byte makes decryption fail; a file
           never decrypts partially into the output (it is written to a temporary file and renamed when complete).

Keys: `opencrawl vault keygen` prints a random key as ocv1:<base64url> (store it as the OPENCRAWL_DATA_KEY secret).
A passphrase (16+ characters) works too and is stretched with scrypt (N=2^17, r=8, p=1).
"""
from __future__ import annotations

import base64
import os
import secrets
import struct
import tarfile
import tempfile
from pathlib import Path

MAGIC = b"OCVAULT1"
VERSION = 1
KDF_HKDF, KDF_SCRYPT = 1, 2
CHUNK = 1 << 20
TAG = 16
HEADER_LEN = 8 + 1 + 1 + 16 + 7 + 4 + 3
SCRYPT = (17, 8, 1)              # log2(N), r, p
KEY_PREFIX = "ocv1:"
MIN_PASSPHRASE = 16


class VaultError(Exception):
    """Wrong key, damaged or truncated file, or not a vault file."""


def _aead():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - cryptography is a dependency
        raise VaultError("the `cryptography` package is needed: pip install cryptography") from exc
    return AESGCM


def keygen() -> str:
    return KEY_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _parse_key(key: str) -> tuple[int, bytes]:
    key = (key or "").strip()
    if key.startswith(KEY_PREFIX):
        raw = key[len(KEY_PREFIX):]
        try:
            b = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError as exc:
            raise VaultError("the key is not valid base64url") from exc
        if len(b) != 32:
            raise VaultError("an ocv1: key must hold 32 bytes")
        return KDF_HKDF, b
    if len(key) < MIN_PASSPHRASE:
        raise VaultError(f"use a key from `opencrawl vault keygen` or a passphrase of {MIN_PASSPHRASE}+ characters")
    return KDF_SCRYPT, key.encode("utf-8")


def _derive(kdf: int, secret: bytes, salt: bytes, params: tuple[int, int, int]) -> bytes:
    if kdf == KDF_HKDF:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"opencrawl vault v1").derive(secret)
    if kdf == KDF_SCRYPT:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        log_n, r, p = params
        if not (14 <= log_n <= 22 and 1 <= r <= 32 and 1 <= p <= 16):
            raise VaultError("unreasonable scrypt parameters in the header")
        return Scrypt(salt=salt, length=32, n=1 << log_n, r=r, p=p).derive(secret)
    raise VaultError("unknown key derivation in the header")


def key_from_env(name: str = "OPENCRAWL_DATA_KEY") -> str:
    key = os.environ.get(name, "")
    if not key:
        raise VaultError(f"{name} is not set")
    return key


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    if counter >= 1 << 32:
        raise VaultError("file too large for one vault")
    return prefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")


def encrypt_stream(src, dst, key: str, chunk: int = CHUNK) -> int:
    """Encrypt everything readable from `src` into `dst`. Returns plaintext bytes."""
    kdf, secret = _parse_key(key)
    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(7)
    params = SCRYPT if kdf == KDF_SCRYPT else (0, 0, 0)
    header = MAGIC + bytes([VERSION, kdf]) + salt + prefix + struct.pack(">I", chunk) + bytes(params)
    aead = _aead()(_derive(kdf, secret, salt, params))
    dst.write(header)
    total, counter = 0, 0
    buf = src.read(chunk)
    while True:
        nxt = src.read(chunk) if len(buf) == chunk else b""
        last = not nxt
        dst.write(aead.encrypt(_nonce(prefix, counter, last), buf, header))
        total += len(buf)
        counter += 1
        if last:
            return total
        buf = nxt


def decrypt_stream(src, dst, key: str) -> int:
    header = src.read(HEADER_LEN)
    if len(header) < HEADER_LEN or not header.startswith(MAGIC):
        raise VaultError("not an OpenCrawl vault file")
    version, kdf = header[8], header[9]
    if version != VERSION:
        raise VaultError(f"vault version {version} is not supported")
    salt, prefix = header[10:26], header[26:33]
    chunk = struct.unpack(">I", header[33:37])[0]
    params = tuple(header[37:40])
    if not (1024 <= chunk <= 64 << 20):
        raise VaultError("unreasonable chunk size in the header")
    kind, secret = _parse_key(key)
    if kind != kdf:
        raise VaultError("this file was sealed with a different kind of key (key vs passphrase)")
    from cryptography.exceptions import InvalidTag

    aead = _aead()(_derive(kdf, secret, salt, params))
    total, counter = 0, 0
    sealed = src.read(chunk + TAG)
    while True:
        nxt = src.read(chunk + TAG) if len(sealed) == chunk + TAG else b""
        last = not nxt
        if len(sealed) < TAG:
            raise VaultError("the file is truncated")
        try:
            plain = aead.decrypt(_nonce(prefix, counter, last), sealed, header)
        except InvalidTag:
            raise VaultError("wrong key, or the file is damaged or truncated") from None
        dst.write(plain)
        total += len(plain)
        counter += 1
        if last:
            return total
        sealed = nxt


def _private_open(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "wb")


def encrypt_file(src: Path, dst: Path, key: str) -> int:
    src, dst = Path(src), Path(dst)
    tmp = dst.with_name(dst.name + ".part")
    with open(src, "rb") as fi, _private_open(tmp) as fo:
        n = encrypt_stream(fi, fo, key)
    tmp.replace(dst)
    return n


def decrypt_file(src: Path, dst: Path, key: str) -> int:
    src, dst = Path(src), Path(dst)
    tmp = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as fi, _private_open(tmp) as fo:
            n = decrypt_stream(fi, fo, key)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dst)
    return n


# ----------------------------------------------------------------------------- directories (CI data archive)
def pack(paths: list[Path], dst: Path, key: str, base: Path, exclude: tuple[str, ...] = ()) -> dict:
    """tar (gzip) the given files/directories, relative to `base`, and encrypt the archive into `dst`."""
    base = Path(base).resolve()
    buf = tempfile.TemporaryFile()        # the plain archive never sits anywhere but an unlinked private file
    n = 0
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for p in paths:
            p = Path(p).resolve()
            if not p.exists():
                continue
            for f in sorted([p] if p.is_file() else p.rglob("*")):
                rel = f.relative_to(base).as_posix()
                if f.is_symlink() or not f.is_file() or any(rel == e or rel.startswith(e.rstrip("/") + "/")
                                                             for e in exclude):
                    continue
                info = tar.gettarinfo(str(f), arcname=rel)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o600
                with open(f, "rb") as fh:
                    tar.addfile(info, fh)
                n += 1
    buf.seek(0)
    tmp = Path(dst).with_name(Path(dst).name + ".part")
    try:
        with _private_open(tmp) as fo:
            size = encrypt_stream(buf, fo, key)
    finally:
        buf.close()
    tmp.replace(dst)
    return {"files": n, "bytes": size, "out": str(dst)}


def unpack(src: Path, dest: Path, key: str) -> dict:
    """Decrypt and extract an archive made by pack() into `dest`. Only regular files and directories inside
    `dest` are written (no absolute paths, '..', links or devices), each owner-readable only."""
    buf = tempfile.TemporaryFile()
    with open(src, "rb") as fi:
        decrypt_stream(fi, buf, key)
    buf.seek(0)
    dest = Path(dest).resolve()
    n = 0
    with buf, tarfile.open(fileobj=buf, mode="r:gz") as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in Path(m.name).parts \
                    or not str(target).startswith(str(dest) + os.sep):
                raise VaultError(f"refusing archive member {m.name!r}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            fh = tar.extractfile(m)
            with _private_open(target) as out:
                while True:
                    b = fh.read(1 << 20)
                    if not b:
                        break
                    out.write(b)
            n += 1
    return {"files": n, "dest": str(dest)}

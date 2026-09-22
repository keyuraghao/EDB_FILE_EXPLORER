"""Project files: share a set of open databases (and the workspace around them) between machines.

A project is a ZIP container (``.edbproj``) with:

* ``manifest.json`` - the databases (original path, size, SHA-256, format) and the workspace (open tabs,
  layout, notes).  With a password it is stored encrypted as ``manifest.enc`` instead.
* ``evidence/<n>`` - optionally the database files themselves, so the project is self-contained
  (encrypted too when a password is set, in 4 MiB AES-256-GCM chunks so files of any size stream).
* ``hashes.json`` - the **integrity hash map**: SHA-256 of every member as stored, plus the SHA-256 of each
  original evidence file.  Every member is verified against it before use.
* ``signature.json`` - an **Ed25519 signature** over ``hashes.json`` (and therefore over everything),
  with the signer's public key and name.  Keys live in the user's config directory and are created on
  first use; recipients keep a trust store of known public-key fingerprints.

Confidentiality: AES-256-GCM with a key derived from the password by scrypt (random salt).  The
encryption happens before signing, so a recipient can verify who made the project without the password.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import io
import json
import logging
import os
import platform
import posixpath
import secrets
import socket
import struct
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edb_explorer import __version__
from edb_explorer.core.database import EdbDatabase
from edb_explorer.core.exceptions import EdbExplorerError

log = logging.getLogger(__name__)

__all__ = [
    "EXTENSION",
    "FORMAT",
    "ImportResult",
    "IntegrityError",
    "Project",
    "ProjectDatabase",
    "ProjectError",
    "ProjectHeader",
    "SignatureInfo",
    "SigningIdentity",
    "TrustStore",
    "config_dir",
    "export_project",
    "import_project",
    "inspect_project",
    "resolve_evidence",
    "sha256_file",
]

EXTENSION = ".edbproj"
FORMAT = "edb-explorer-project"
FORMAT_VERSION = 1
CHUNK = 4 * 1024 * 1024
_MAGIC = b"EDBP"  # prefix of encrypted members
_MANIFEST, _MANIFEST_ENC, _HASHES, _SIGNATURE, _CRYPTO = (
    "manifest.json",
    "manifest.enc",
    "hashes.json",
    "signature.json",
    "crypto.json",
)
ProgressCallback = Callable[[str, int, int], bool]  # (message, done, total) -> keep going?


class ProjectError(EdbExplorerError):
    """Malformed project, wrong password, unsupported version."""


class IntegrityError(ProjectError):
    """A member's content does not match the signed hash map."""


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ProjectDatabase:
    id: str
    path: str  # original absolute path on the exporting machine
    name: str
    size: int
    sha256: str
    kind: str
    kind_name: str = ""
    relative: str = ""  # path relative to the common evidence root, POSIX style
    embedded: str | None = None  # member name inside the project when the file travels with it


@dataclass(slots=True)
class Project:
    name: str
    notes: str = ""
    created: str = ""
    app_version: str = __version__
    author: dict[str, str] = field(default_factory=dict)
    evidence_root: str = ""
    databases: list[ProjectDatabase] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["format"] = FORMAT
        d["format_version"] = FORMAT_VERSION
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Project:
        if d.get("format") != FORMAT:
            raise ProjectError("Not an EDB Explorer project manifest")
        if int(d.get("format_version", 0)) > FORMAT_VERSION:
            raise ProjectError(f"Project format {d.get('format_version')} is newer than this version understands")
        dbs = [
            ProjectDatabase(**{k: v for k, v in x.items() if k in ProjectDatabase.__slots__}) for x in d["databases"]
        ]
        return cls(
            name=d.get("name", ""),
            notes=d.get("notes", ""),
            created=d.get("created", ""),
            app_version=d.get("app_version", ""),
            author=dict(d.get("author") or {}),
            evidence_root=d.get("evidence_root", ""),
            databases=dbs,
            workspace=dict(d.get("workspace") or {}),
        )


@dataclass(slots=True)
class SignatureInfo:
    present: bool
    valid: bool | None  # None when not present
    algorithm: str = ""
    signer: dict[str, str] = field(default_factory=dict)
    public_key: str = ""  # base64 raw Ed25519 public key
    fingerprint: str = ""  # SHA-256 of the public key, hex, grouped
    trusted: bool = False
    trusted_as: str = ""

    @property
    def summary(self) -> str:
        if not self.present:
            return "not signed"
        if not self.valid:
            return "INVALID signature - the project was altered after signing or the key does not match"
        who = self.signer.get("name") or "unknown signer"
        host = self.signer.get("host")
        return (
            f"signed by {who}"
            + (f" on {host}" if host else "")
            + (f" - trusted ({self.trusted_as})" if self.trusted else " - signer not in your trust store")
        )


@dataclass(slots=True)
class ProjectHeader:
    """What can be known about a project file before decrypting it."""

    path: str
    encrypted: bool
    signature: SignatureInfo
    integrity_ok: bool
    integrity_errors: list[str]
    members: list[str]
    size: int
    project: Project | None = None  # filled when the manifest is not encrypted


@dataclass(slots=True)
class ImportResult:
    header: ProjectHeader
    project: Project
    evidence: dict[str, str | None]  # db id -> local path (None when not found)
    verified: dict[str, bool | None]  # db id -> evidence SHA-256 matched (None = not checked)
    extracted: list[str]


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def sha256_file(path: str | os.PathLike[str], progress: Callable[[int], bool] | None = None) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
            done += len(chunk)
            if progress and not progress(done):
                raise ProjectError("Cancelled")
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Encryption (AES-256-GCM, scrypt)
# --------------------------------------------------------------------------- #
def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))


def _encrypt_stream(key: bytes, src: io.BufferedReader | io.BytesIO, dst: Any, member: str) -> None:
    """``EDBP`` + 8-byte nonce prefix + chunks of ``[u32 length][ciphertext+tag]``; chunk i uses nonce
    ``prefix || i`` and the member name + index as associated data, so chunks cannot be reordered or swapped
    between members."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aead = AESGCM(key)
    prefix = secrets.token_bytes(8)
    dst.write(_MAGIC + prefix)
    index = 0
    while True:
        chunk = src.read(CHUNK)
        ct = aead.encrypt(prefix + struct.pack(">I", index), chunk, f"{member}:{index}".encode())
        dst.write(struct.pack(">I", len(ct)) + ct)
        if len(chunk) < CHUNK:  # the last (possibly empty) chunk is always written so truncation is detected
            break
        index += 1


def _decrypt_stream(key: bytes, src: Any, dst: Any, member: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aead = AESGCM(key)
    head = src.read(12)
    if head[:4] != _MAGIC:
        raise ProjectError(f"{member} is not an encrypted member")
    prefix = head[4:]
    index = 0
    while True:
        raw = src.read(4)
        if len(raw) < 4:
            raise IntegrityError(f"{member}: truncated")
        (n,) = struct.unpack(">I", raw)
        ct = src.read(n)
        try:
            chunk = aead.decrypt(prefix + struct.pack(">I", index), ct, f"{member}:{index}".encode())
        except InvalidTag as exc:
            raise ProjectError("Wrong password, or the encrypted content was altered") from exc
        dst.write(chunk)
        if len(chunk) < CHUNK:
            break
        index += 1


# --------------------------------------------------------------------------- #
# Signing (Ed25519) and trust
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    env = os.environ.get("EDB_EXPLORER_CONFIG_DIR")
    if env:
        base = Path(env)
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "EDB Explorer"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "edb-explorer"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fingerprint(public_raw: bytes) -> str:
    hexd = hashlib.sha256(public_raw).hexdigest()[:32]
    return ":".join(hexd[i : i + 4] for i in range(0, len(hexd), 4))


class SigningIdentity:
    """This user's Ed25519 key pair (``signing_key.pem`` in the config directory, created on first use)."""

    def __init__(self, private_key: Any, name: str, host: str) -> None:
        self._key = private_key
        self.name = name
        self.host = host

    @classmethod
    def load_or_create(cls, directory: Path | None = None, name: str | None = None) -> SigningIdentity:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        directory = directory or config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        pem = directory / "signing_key.pem"
        meta = directory / "signing_key.json"
        if pem.exists():
            key = serialization.load_pem_private_key(pem.read_bytes(), password=None)
            info = json.loads(meta.read_text()) if meta.exists() else {}
        else:
            key = Ed25519PrivateKey.generate()
            pem.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            try:
                os.chmod(pem, 0o600)
            except OSError:
                pass
            info = {"name": name or _default_user(), "host": socket.gethostname(), "created": _now()}
            meta.write_text(json.dumps(info, indent=2))
        if name:
            info["name"] = name
            meta.write_text(json.dumps(info, indent=2))
        return cls(key, info.get("name") or _default_user(), info.get("host") or socket.gethostname())

    @property
    def public_raw(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public_raw).decode()

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.public_raw)

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)


def _default_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _verify(public_b64: str, signature_b64: str, data: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(base64.b64decode(signature_b64), data)
        return True
    except (InvalidSignature, ValueError):
        return False


class TrustStore:
    """Known signer fingerprints (``trusted_signers.json`` in the config directory)."""

    def __init__(self, directory: Path | None = None) -> None:
        directory = directory or config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "trusted_signers.json"
        self._entries: dict[str, dict[str, str]] = {}
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._entries = {}

    def lookup(self, fingerprint: str) -> dict[str, str] | None:
        return self._entries.get(fingerprint)

    def trust(self, fingerprint: str, name: str, public_key: str = "") -> None:
        self._entries[fingerprint] = {"name": name, "public_key": public_key, "added": _now()}
        self.path.write_text(json.dumps(self._entries, indent=2))

    def forget(self, fingerprint: str) -> None:
        if self._entries.pop(fingerprint, None) is not None:
            self.path.write_text(json.dumps(self._entries, indent=2))

    def entries(self) -> dict[str, dict[str, str]]:
        return dict(self._entries)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def _common_root(paths: Sequence[Path]) -> Path | None:
    if not paths:
        return None
    try:
        common = Path(os.path.commonpath([str(p.parent) for p in paths]))
    except ValueError:  # different drives on Windows
        return None
    return common


def export_project(
    databases: Iterable[EdbDatabase],
    output: str | os.PathLike[str],
    *,
    name: str = "",
    notes: str = "",
    workspace: dict[str, Any] | None = None,
    password: str | None = None,
    embed: bool = False,
    identity: SigningIdentity | None = None,
    progress: ProgressCallback | None = None,
) -> Project:
    """Write ``output``; returns the manifest that was stored.  ``identity=None`` leaves the project unsigned."""
    dbs = list(databases)
    if not dbs:
        raise ProjectError("Nothing to export: no database is open")
    out = Path(output)
    if out.suffix.lower() != EXTENSION:
        out = out.with_suffix(out.suffix + EXTENSION)
    root = _common_root([db.path for db in dbs])
    total = sum(db.path.stat().st_size if db.path.is_file() else 0 for db in dbs)
    project = Project(
        name=name or out.stem,
        notes=notes,
        created=_now(),
        author={"user": _default_user(), "host": socket.gethostname(), "platform": platform.platform()},
        evidence_root=str(root) if root else "",
        workspace=dict(workspace or {}),
    )
    hashes: dict[str, str] = {}
    evidence_hashes: dict[str, str] = {}
    key = salt = None
    if password:
        salt = secrets.token_bytes(16)
        key = _derive_key(password, salt)

    done = 0

    def report(msg: str) -> None:
        if progress and not progress(msg, done, total):
            raise ProjectError("Cancelled")

    tmp = out.with_suffix(out.suffix + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, db in enumerate(dbs):
                if not db.path.is_file():
                    raise ProjectError(f"{db.path} is a directory store; only single-file databases can be exported")
                report(f"Hashing {db.path.name}")
                digest = sha256_file(db.path)
                evidence_hashes[db.id] = digest
                try:
                    rel = db.path.relative_to(root).as_posix() if root else db.path.name
                except ValueError:
                    rel = db.path.name
                member = None
                if embed:
                    member = f"evidence/{i:03d}_{db.path.name}"
                    report(f"Adding {db.path.name}")
                    _add_file(zf, member, db.path, key, hashes)
                    done += db.path.stat().st_size
                project.databases.append(
                    ProjectDatabase(
                        id=db.id,
                        path=str(db.path),
                        name=db.path.name,
                        size=db.path.stat().st_size,
                        sha256=digest,
                        kind=db.kind,
                        kind_name=db.info.kind_name,
                        relative=rel,
                        embedded=member,
                    )
                )
            manifest = _canonical(project.to_dict())
            if key is not None:
                buf = io.BytesIO()
                _encrypt_stream(key, io.BytesIO(manifest), buf, _MANIFEST_ENC)
                data = buf.getvalue()
                zf.writestr(_MANIFEST_ENC, data)
                hashes[_MANIFEST_ENC] = _sha256_bytes(data)
                crypto = _canonical(
                    {
                        "cipher": "AES-256-GCM",
                        "kdf": "scrypt",
                        "n": 2**15,
                        "r": 8,
                        "p": 1,
                        "salt": base64.b64encode(salt or b"").decode(),
                    }
                )
                zf.writestr(_CRYPTO, crypto)
                hashes[_CRYPTO] = _sha256_bytes(crypto)
            else:
                zf.writestr(_MANIFEST, manifest)
                hashes[_MANIFEST] = _sha256_bytes(manifest)
            hash_doc = _canonical(
                {
                    "algorithm": "sha256",
                    "members": hashes,
                    "evidence": evidence_hashes,
                    "created": project.created,
                    "app_version": __version__,
                }
            )
            zf.writestr(_HASHES, hash_doc)
            if identity is not None:
                sig = {
                    "algorithm": "Ed25519",
                    "signed": _HASHES,
                    "public_key": identity.public_b64,
                    "fingerprint": identity.fingerprint,
                    "signer": {"name": identity.name, "host": identity.host, "signed_at": _now()},
                    "signature": base64.b64encode(identity.sign(hash_doc)).decode(),
                }
                zf.writestr(_SIGNATURE, _canonical(sig))
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return project


class _HashingWriter:
    """Write-through wrapper that keeps a SHA-256 of everything written."""

    def __init__(self, fh: Any) -> None:
        self.fh = fh
        self.hash = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.hash.update(data)
        return self.fh.write(data)

    def hexdigest(self) -> str:
        return self.hash.hexdigest()


def _copy(src: Any, dst: _HashingWriter) -> None:
    for chunk in iter(lambda: src.read(CHUNK), b""):
        dst.write(chunk)


def _add_file(zf: zipfile.ZipFile, member: str, path: Path, key: bytes | None, hashes: dict[str, str]) -> None:
    with open(path, "rb") as src, zf.open(member, "w", force_zip64=True) as raw:
        dst = _HashingWriter(raw)
        if key is None:
            _copy(src, dst)
        else:
            _encrypt_stream(key, src, dst, member)
    hashes[member] = dst.hexdigest()


# --------------------------------------------------------------------------- #
# Inspect / import
# --------------------------------------------------------------------------- #
def _read(zf: zipfile.ZipFile, member: str) -> bytes:
    try:
        return zf.read(member)
    except KeyError as exc:
        raise ProjectError(f"Project is missing {member}") from exc


def inspect_project(path: str | os.PathLike[str], trust: TrustStore | None = None) -> ProjectHeader:
    """Verify the hash map and the signature; read the manifest when it is not encrypted."""
    p = Path(path)
    if not zipfile.is_zipfile(p):
        raise ProjectError(f"{p} is not an EDB Explorer project file")
    with zipfile.ZipFile(p) as zf:
        members = zf.namelist()
        hash_doc = _read(zf, _HASHES)
        try:
            hashes = json.loads(hash_doc)
            expected: dict[str, str] = dict(hashes["members"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProjectError("Corrupt hash map") from exc
        errors: list[str] = []
        for member in members:
            if member in (_HASHES, _SIGNATURE):
                continue
            if member not in expected:
                errors.append(f"{member}: not covered by the hash map")
                continue
            h = hashlib.sha256()
            with zf.open(member) as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    h.update(chunk)
            if h.hexdigest() != expected[member]:
                errors.append(f"{member}: content does not match its recorded hash")
        for member in expected:
            if member not in members:
                errors.append(f"{member}: listed in the hash map but missing")
        signature = SignatureInfo(present=False, valid=None)
        if _SIGNATURE in members:
            try:
                sig = json.loads(_read(zf, _SIGNATURE))
                public = str(sig["public_key"])
                valid = sig.get("algorithm") == "Ed25519" and _verify(public, str(sig["signature"]), hash_doc)
                fp = _fingerprint(base64.b64decode(public))
                signature = SignatureInfo(
                    present=True,
                    valid=valid,
                    algorithm=str(sig.get("algorithm", "")),
                    signer=dict(sig.get("signer") or {}),
                    public_key=public,
                    fingerprint=fp,
                )
                if valid and trust is not None and (known := trust.lookup(fp)) is not None:
                    signature.trusted = True
                    signature.trusted_as = known.get("name", "")
            except (ValueError, KeyError, TypeError):
                signature = SignatureInfo(present=True, valid=False)
        encrypted = _MANIFEST_ENC in members
        project = None
        if not encrypted and _MANIFEST in members and not errors:
            project = Project.from_dict(json.loads(_read(zf, _MANIFEST)))
        return ProjectHeader(
            path=str(p),
            encrypted=encrypted,
            signature=signature,
            integrity_ok=not errors,
            integrity_errors=errors,
            members=members,
            size=p.stat().st_size,
            project=project,
        )


def _open_manifest(zf: zipfile.ZipFile, header: ProjectHeader, password: str | None) -> tuple[Project, bytes | None]:
    if not header.encrypted:
        return Project.from_dict(json.loads(_read(zf, _MANIFEST))), None
    if not password:
        raise ProjectError("This project is encrypted - a password is required")
    crypto = json.loads(_read(zf, _CRYPTO))
    key = _derive_key(password, base64.b64decode(crypto["salt"]))
    buf = io.BytesIO()
    _decrypt_stream(key, io.BytesIO(_read(zf, _MANIFEST_ENC)), buf, _MANIFEST_ENC)
    return Project.from_dict(json.loads(buf.getvalue())), key


def resolve_evidence(
    project: Project, search_roots: Sequence[str | os.PathLike[str]], verify: bool = True
) -> tuple[dict[str, str | None], dict[str, bool | None]]:
    """Find each database on this machine: original path, then ``root/relative``, then any file with the
    same name under a root (walked); with ``verify`` a candidate only counts when its SHA-256 matches."""
    found: dict[str, str | None] = {}
    verified: dict[str, bool | None] = {}
    roots = [Path(r) for r in search_roots]
    name_index: dict[str, list[Path]] | None = None

    def matches(db: ProjectDatabase, candidate: Path) -> bool:
        if not candidate.is_file():
            return False
        if not verify:
            return True
        return sha256_file(candidate) == db.sha256

    for db in project.databases:
        candidates: list[Path] = [Path(db.path)]
        for r in roots:
            candidates.append(r / db.relative)
            candidates.append(r / db.name)
        hit = next((c for c in candidates if matches(db, c)), None)
        if hit is None and roots:
            if name_index is None:
                name_index = {}
                for r in roots:
                    for dirpath, _dirs, files in os.walk(r):
                        for f in files:
                            name_index.setdefault(f, []).append(Path(dirpath) / f)
            hit = next((c for c in name_index.get(db.name, []) if matches(db, c)), None)
        found[db.id] = str(hit) if hit else None
        verified[db.id] = (True if verify else None) if hit else None
    return found, verified


def import_project(
    path: str | os.PathLike[str],
    *,
    password: str | None = None,
    search_roots: Sequence[str | os.PathLike[str]] = (),
    extract_to: str | os.PathLike[str] | None = None,
    trust: TrustStore | None = None,
    require_integrity: bool = True,
    progress: ProgressCallback | None = None,
) -> ImportResult:
    """Verify, decrypt, extract embedded evidence (to ``extract_to``) and locate the rest.

    Embedded files are written under ``extract_to/<project name>/`` and their SHA-256 checked against the
    manifest; files that are not embedded are searched in ``search_roots`` (see :func:`resolve_evidence`).
    """
    header = inspect_project(path, trust)
    if require_integrity and not header.integrity_ok:
        raise IntegrityError("Integrity check failed:\n" + "\n".join(header.integrity_errors))
    if header.signature.present and header.signature.valid is False and require_integrity:
        raise IntegrityError("The project's signature is invalid")
    extracted: list[str] = []
    with zipfile.ZipFile(path) as zf:
        project, key = _open_manifest(zf, header, password)
        evidence: dict[str, str | None] = {}
        verified: dict[str, bool | None] = {}
        embedded = [db for db in project.databases if db.embedded]
        if embedded:
            if extract_to is None:
                extract_to = tempfile.mkdtemp(prefix="edb-project-")
            dest_root = Path(extract_to) / _safe_name(project.name or Path(path).stem)
            dest_root.mkdir(parents=True, exist_ok=True)
            total = sum(db.size for db in embedded)
            done = 0
            for db in embedded:
                assert db.embedded
                target = dest_root / (db.relative.replace("/", os.sep) if db.relative else db.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if progress and not progress(f"Extracting {db.name}", done, total):
                    raise ProjectError("Cancelled")
                with zf.open(db.embedded) as src, open(target, "wb") as raw:
                    dst = _HashingWriter(raw)
                    if key is None:
                        _copy(src, dst)
                    else:
                        _decrypt_stream(key, src, dst, db.embedded)
                if dst.hexdigest() != db.sha256:
                    target.unlink(missing_ok=True)
                    raise IntegrityError(f"{db.name}: extracted file does not match the recorded SHA-256")
                evidence[db.id] = str(target)
                verified[db.id] = True
                extracted.append(str(target))
                done += db.size
        rest = Project(name=project.name, databases=[db for db in project.databases if not db.embedded])
        if rest.databases:
            if progress and not progress("Locating evidence files", 0, 0):
                raise ProjectError("Cancelled")
            f, v = resolve_evidence(rest, search_roots)
            evidence.update(f)
            verified.update(v)
    header.project = project
    return ImportResult(header=header, project=project, evidence=evidence, verified=verified, extracted=extracted)


def _safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_. " else "_" for c in name).strip(" .")
    return posixpath.basename(keep) or "project"

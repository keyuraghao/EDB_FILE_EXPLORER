"""Project files: round-trip, encryption, tamper detection, signing / trust, evidence resolution."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from edb_explorer.core import Session
from edb_explorer.core.backends.ese import ESE_MAGIC
from edb_explorer.core.project import (
    EXTENSION,
    IntegrityError,
    ProjectError,
    SigningIdentity,
    TrustStore,
    export_project,
    import_project,
    inspect_project,
    resolve_evidence,
    sha256_file,
)


@pytest.fixture
def evidence(tmp_path: Path, fake_backend: None) -> dict[str, Path]:
    root = tmp_path / "case"
    (root / "host1").mkdir(parents=True)
    (root / "host2").mkdir()
    a = root / "host1" / "SRUDB.dat"
    b = root / "host2" / "ntds.dat"
    payload = b"\x00\x00\x00\x00" + ESE_MAGIC + b"\x00" * 4088
    a.write_bytes(payload)
    b.write_bytes(payload + b"\x01" * 10_000)  # different content -> different hash
    return {"a": a, "b": b, "root": root}


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    return d


def _open(evidence: dict[str, Path]) -> Session:
    s = Session()
    s.open(evidence["a"])
    s.open(evidence["b"])
    return s


def test_export_import_round_trip_plain(evidence: dict[str, Path], keys: Path, tmp_path: Path) -> None:
    session = _open(evidence)
    try:
        out = tmp_path / "case"  # extension is added
        ws = {"tabs": [{"kind": "table", "db": "srudb", "table": "SruDbIdMapTable"}], "current": 0}
        project = export_project(session, out, name="Case 42", notes="hello", workspace=ws)
        path = out.with_suffix(EXTENSION)
        assert path.exists() and project.name == "Case 42" and len(project.databases) == 2
        assert [d.relative for d in project.databases] == ["host1/SRUDB.dat", "host2/ntds.dat"]
        assert project.evidence_root == str(evidence["root"])
        assert project.databases[0].sha256 == sha256_file(evidence["a"]) and project.databases[0].embedded is None

        header = inspect_project(path)
        assert header.integrity_ok and not header.encrypted and not header.signature.present
        assert header.project is not None and header.project.workspace == ws
        assert sorted(header.members) == ["hashes.json", "manifest.json"]

        res = import_project(path)  # original paths still exist -> found and verified
        assert res.evidence == {"srudb": str(evidence["a"]), "ntds": str(evidence["b"])}
        assert res.verified == {"srudb": True, "ntds": True} and res.extracted == []
        assert res.project.notes == "hello" and res.project.author["user"]
    finally:
        session.close_all()


def test_resolve_moved_evidence_by_relative_path_name_and_hash(evidence: dict[str, Path], tmp_path: Path) -> None:
    session = _open(evidence)
    try:
        path = tmp_path / ("moved" + EXTENSION)
        export_project(session, path, name="moved")
    finally:
        session.close_all()
    # simulate the other computer: evidence tree copied elsewhere, originals gone
    other = tmp_path / "other-pc" / "evidence"
    shutil.copytree(evidence["root"], other)
    shutil.rmtree(evidence["root"])
    header = inspect_project(path)
    assert header.project is not None
    found, verified = resolve_evidence(header.project, [other])
    assert found == {"srudb": str(other / "host1" / "SRUDB.dat"), "ntds": str(other / "host2" / "ntds.dat")}
    assert verified == {"srudb": True, "ntds": True}
    # renamed folder layout: only the file names survive -> found by walking + hash
    flat = tmp_path / "flat"
    flat.mkdir()
    shutil.move(str(other / "host2" / "ntds.dat"), flat / "ntds.dat")
    (flat / "decoy").mkdir()
    (flat / "decoy" / "SRUDB.dat").write_bytes(b"not the same file")  # same name, wrong hash -> skipped
    found, verified = resolve_evidence(header.project, [flat])
    assert found == {"srudb": None, "ntds": str(flat / "ntds.dat")} and verified["srudb"] is None
    res = import_project(path, search_roots=[flat, other])
    assert res.evidence["ntds"] == str(flat / "ntds.dat") and res.evidence["srudb"] == str(
        other / "host1" / "SRUDB.dat"
    )


def test_encrypted_and_signed_project(evidence: dict[str, Path], keys: Path, tmp_path: Path) -> None:
    session = _open(evidence)
    identity = SigningIdentity.load_or_create(keys, name="Analyst One")
    assert (keys / "signing_key.pem").exists() and identity.fingerprint.count(":") == 7
    assert SigningIdentity.load_or_create(keys).fingerprint == identity.fingerprint  # stable across runs
    path = tmp_path / ("secret" + EXTENSION)
    try:
        export_project(session, path, name="secret", password="hunter2", embed=True, identity=identity)
    finally:
        session.close_all()
    with zipfile.ZipFile(path) as zf:
        names = sorted(zf.namelist())
        assert names == [
            "crypto.json",
            "evidence/000_SRUDB.dat",
            "evidence/001_ntds.dat",
            "hashes.json",
            "manifest.enc",
            "signature.json",
        ]
        assert zf.read("manifest.enc")[:4] == b"EDBP" and b"Case" not in zf.read("manifest.enc")
        assert zf.read("evidence/000_SRUDB.dat")[:4] == b"EDBP"  # evidence is encrypted too
        assert ESE_MAGIC not in zf.read("evidence/000_SRUDB.dat")

    header = inspect_project(path)
    assert header.encrypted and header.project is None and header.integrity_ok
    sig = header.signature
    assert sig.present and sig.valid and sig.signer["name"] == "Analyst One" and sig.fingerprint == identity.fingerprint
    assert not sig.trusted and "not in your trust store" in sig.summary

    trust = TrustStore(keys)
    trust.trust(sig.fingerprint, "Analyst One (verified by phone)", sig.public_key)
    header = inspect_project(path, trust)
    assert header.signature.trusted and header.signature.trusted_as == "Analyst One (verified by phone)"
    assert TrustStore(keys).lookup(sig.fingerprint)["name"].startswith("Analyst One")

    with pytest.raises(ProjectError, match="password is required"):
        import_project(path)
    with pytest.raises(ProjectError, match="Wrong password"):
        import_project(path, password="nope")

    shutil.rmtree(evidence["root"])  # the other computer has no copy: rely on the embedded files
    dest = tmp_path / "extracted"
    res = import_project(path, password="hunter2", extract_to=dest, trust=trust)
    assert res.header.signature.trusted and res.project.name == "secret"
    assert res.extracted == [str(dest / "secret" / "host1" / "SRUDB.dat"), str(dest / "secret" / "host2" / "ntds.dat")]
    assert res.verified == {"srudb": True, "ntds": True}
    assert (dest / "secret" / "host2" / "ntds.dat").read_bytes()[-5:] == b"\x01" * 5
    assert sha256_file(res.evidence["ntds"]) == res.project.databases[1].sha256
    session2 = Session()
    try:
        db = session2.open(res.evidence["srudb"], db_id="srudb")
        assert db.id == "srudb" and db.info.table_count > 0
    finally:
        session2.close_all()


def _tamper(path: Path, member: str, mutate: Any) -> None:
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            dst.writestr(name, mutate(data) if name == member else data)
    tmp.replace(path)


def test_tampering_is_detected(evidence: dict[str, Path], keys: Path, tmp_path: Path) -> None:
    session = _open(evidence)
    identity = SigningIdentity.load_or_create(keys)
    path = tmp_path / ("signed" + EXTENSION)
    try:
        export_project(session, path, name="signed", identity=identity, embed=True)
    finally:
        session.close_all()
    assert inspect_project(path).integrity_ok

    # 1. change the manifest (e.g. point at a different file): hash map no longer matches
    edited = path.with_name("edited" + EXTENSION)
    shutil.copy(path, edited)
    _tamper(edited, "manifest.json", lambda d: d.replace(b'"notes":""', b'"notes":"planted"'))
    h = inspect_project(edited)
    assert not h.integrity_ok and h.integrity_errors == ["manifest.json: content does not match its recorded hash"]
    assert h.signature.valid  # the signature itself (over hashes.json) is intact, but the content is not
    with pytest.raises(IntegrityError, match="Integrity check failed"):
        import_project(edited)

    # 2. change an embedded evidence file
    evil = path.with_name("evil" + EXTENSION)
    shutil.copy(path, evil)
    _tamper(evil, "evidence/001_ntds.dat", lambda d: d[:-1] + b"\x02")
    h = inspect_project(evil)
    assert h.integrity_errors == ["evidence/001_ntds.dat: content does not match its recorded hash"]

    # 3. re-hash after editing (an attacker fixing the hash map): the signature breaks instead
    forged = path.with_name("forged" + EXTENSION)
    shutil.copy(path, forged)
    with zipfile.ZipFile(forged) as zf:
        hashes = json.loads(zf.read("hashes.json"))
    hashes["members"]["manifest.json"] = "0" * 64
    _tamper(forged, "hashes.json", lambda _d: json.dumps(hashes).encode())
    h = inspect_project(forged)
    assert h.signature.present and h.signature.valid is False
    assert "INVALID" in h.signature.summary
    with pytest.raises(IntegrityError, match=r"Integrity check failed|signature is invalid"):
        import_project(forged)

    # 4. an extra member smuggled in is reported as uncovered
    extra = path.with_name("extra" + EXTENSION)
    shutil.copy(path, extra)
    with zipfile.ZipFile(extra, "a") as zf:
        zf.writestr("evidence/999_extra.bin", b"x")
    assert inspect_project(extra).integrity_errors == ["evidence/999_extra.bin: not covered by the hash map"]

    # 5. a signature from a different key does not verify
    other = SigningIdentity.load_or_create(tmp_path / "otherkeys")
    swapped = path.with_name("swapped" + EXTENSION)
    shutil.copy(path, swapped)
    with zipfile.ZipFile(swapped) as zf:
        sig = json.loads(zf.read("signature.json"))
    sig["public_key"] = other.public_b64
    _tamper(swapped, "signature.json", lambda _d: json.dumps(sig).encode())
    assert inspect_project(swapped).signature.valid is False

    # not a project at all
    (tmp_path / "junk.edbproj").write_bytes(b"junk")
    with pytest.raises(ProjectError, match="not an EDB Explorer project"):
        inspect_project(tmp_path / "junk.edbproj")


def test_cli_project_commands(evidence: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from edb_explorer.cli import app

    monkeypatch.setenv("EDB_EXPLORER_CONFIG_DIR", str(tmp_path / "cfg"))
    runner = CliRunner()
    out = tmp_path / "cli"
    r = runner.invoke(
        app,
        ["project", "export", str(out), str(evidence["a"]), str(evidence["b"]), "--name", "cli", "-p", "pw", "--embed"],
    )
    assert r.exit_code == 0, r.output
    assert "encrypted, evidence embedded, signed by" in r.output
    r = runner.invoke(app, ["project", "info", str(out) + EXTENSION])
    assert r.exit_code == 0 and "Integrity: OK" in r.output and "signer not in your trust store" in r.output
    assert "Give --password" in r.output
    r = runner.invoke(app, ["project", "info", str(out) + EXTENSION, "-p", "pw", "--json"])
    assert r.exit_code == 0
    info = json.loads(r.output)
    assert info["integrity_ok"] and info["signature"]["valid"] and len(info["project"]["databases"]) == 2
    fp = info["signature"]["fingerprint"]
    r = runner.invoke(app, ["project", "trust", fp, "--name", "me"])
    assert r.exit_code == 0 and "Trusting" in r.output
    r = runner.invoke(app, ["project", "trust"])
    assert r.exit_code == 0 and "me" in r.output and fp in r.output
    shutil.rmtree(evidence["root"])
    r = runner.invoke(app, ["project", "import", str(out) + EXTENSION, "-p", "pw", "--dest", str(tmp_path / "dest")])
    assert r.exit_code == 0, r.output
    assert "Integrity: OK" in r.output and "trusted (me)" in r.output and "not found" not in r.output
    assert (tmp_path / "dest" / "cli" / "host1" / "SRUDB.dat").exists()
    r = runner.invoke(app, ["project", "import", str(out) + EXTENSION, "-p", "bad"])
    assert r.exit_code != 0 and "Wrong password" in r.output + (r.stderr if hasattr(r, "stderr") else "")

"""Shared fixtures: a fake dissect backend so the whole core can be tested without real files."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from edb_explorer.core import Session
from edb_explorer.core.backends.ese import ESE_MAGIC

# --------------------------------------------------------------------------- #
# Fake dissect objects
# --------------------------------------------------------------------------- #


class _Coltyp:
    """Mimics dissect's JET_coltyp enum members (has .name and int())."""

    _ids = {
        "Bit": 1,
        "UnsignedByte": 2,
        "Short": 3,
        "Long": 4,
        "Currency": 5,
        "IEEESingle": 6,
        "IEEEDouble": 7,
        "DateTime": 8,
        "Binary": 9,
        "Text": 10,
        "LongBinary": 11,
        "LongText": 12,
        "UnsignedLong": 14,
        "LongLong": 15,
        "GUID": 16,
        "UnsignedShort": 17,
    }

    def __init__(self, name: str) -> None:
        self.name = name

    def __int__(self) -> int:
        return self._ids[self.name]

    def __index__(self) -> int:
        return int(self)


@dataclass
class FakeColumn:
    identifier: int
    name: str
    type_name: str
    storage: str = "fixed"
    size: int | None = 4
    encoding: Any = None

    @property
    def type(self) -> _Coltyp:
        return _Coltyp(self.type_name)

    @property
    def is_fixed(self) -> bool:
        return self.storage == "fixed"

    @property
    def is_variable(self) -> bool:
        return self.storage == "variable"

    @property
    def is_tagged(self) -> bool:
        return self.storage == "tagged"

    @property
    def is_text(self) -> bool:
        return self.type_name in ("Text", "LongText")

    @property
    def is_binary(self) -> bool:
        return self.type_name in ("Binary", "LongBinary")


class _Flags(int):
    Unique = 1


@dataclass
class FakeIndex:
    name: str
    is_primary: bool
    unique: bool
    columns: list[FakeColumn] = field(default_factory=list)

    @property
    def idb_flags(self) -> _Flags:
        return _Flags(1 if self.unique else 0)


class FakeRecord:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def as_dict(self, raw: bool = False) -> dict[str, Any]:
        return dict(self._values)


class FakeTable:
    def __init__(
        self,
        name: str,
        root_page: int,
        columns: list[FakeColumn],
        rows: list[dict[str, Any]],
        indexes: list[FakeIndex] | None = None,
    ) -> None:
        self.name = name
        self.root_page = root_page
        self.columns = columns
        self.indexes = indexes or []
        self._rows = rows

    def records(self) -> Iterator[FakeRecord]:
        for r in self._rows:
            yield FakeRecord(r)


class _Logtime:
    def __init__(self, y: int = 0, mo: int = 0, d: int = 0, h: int = 0, mi: int = 0, s: int = 0) -> None:
        self.bYear, self.bMonth, self.bDay, self.bHours, self.bMinutes, self.bSeconds = y, mo, d, h, mi, s
        self.bMillisecondsLow = 0
        self.bMillisecondsHigh = 0


class _SignDb:
    ulRandom = 0x1234
    logtimeCreate = _Logtime(120, 11, 19, 7, 43, 44)  # 2020-11-19
    szComputerName = b"\x00" * 16


class _Header:
    ulChecksum = 1
    ulVersion = 0x620
    ulDaeUpdateMajor = 20
    ulDaeUpdateMinor = 0
    cbPageSize = 4096
    dbstate = 3
    dbid = 1
    objidLast = 100
    dwMajorVersion = 10
    dwMinorVersion = 0
    dwBuildNumber = 19042
    lSPNumber = 0
    ulCreateVersion = 0x620
    ulCreateUpdate = 20
    lGenMinRequired = 0
    lGenMaxRequired = 0
    lGenMaxCommitted = 0
    ulRepairCount = 0
    ulBadChecksum = 0
    ulECCFixSuccess = 0
    ulECCFixFail = 0
    ulIncrementalReseedCount = 0
    ulPagePatchCount = 0
    logtimeAttach = _Logtime(121, 2, 3, 17, 12, 59)
    logtimeDetach = _Logtime(121, 2, 3, 17, 14, 59)
    logtimeConsistent = _Logtime()
    logtimeRepair = _Logtime()
    logtimeGenMaxCreate = _Logtime()
    signDb = _SignDb()


def ole_bits(days: float) -> int:
    """Pack an OLE date as the int64 bit pattern dissect returns for DateTime columns."""
    return struct.unpack("<q", struct.pack("<d", days))[0]


SID_BYTES = bytes.fromhex("010100000000000512000000")  # S-1-5-18
UTF16_BLOB = "C:\\Windows\\svchost.exe".encode("utf-16-le")

SRUM_ROWS = [
    {"IdType": 0, "IdIndex": 1},
    {"IdType": 3, "IdIndex": 2, "IdBlob": SID_BYTES},
    {"IdType": 0, "IdIndex": 3, "IdBlob": UTF16_BLOB},
    {"IdType": 0, "IdIndex": 4, "IdBlob": "chrome.exe".encode("utf-16-le")},
]
NET_ROWS = [
    {
        "AutoIncId": i,
        "TimeStamp": ole_bits(44220.0 + i / 24),
        "AppId": 10 + i,
        "UserId": 4,
        "BytesSent": 100 * i,
        "BytesRecvd": 7 * i,
    }
    for i in range(1, 26)
]


class FakeEseDB:
    """Stand-in for dissect.esedb.EseDB with a SRUM-like schema."""

    def __init__(self, fh: Any, impacket_compat: bool = False) -> None:
        head = fh.read(8)
        if head[4:8] != ESE_MAGIC:
            from dissect.esedb.exceptions import InvalidDatabase

            raise InvalidDatabase("invalid file header signature")
        self.header = _Header()
        self.page_size = 4096
        self.version = 0x620
        self.format_major = 20
        self._tables = [
            FakeTable(
                "MSysObjects",
                4,
                [FakeColumn(1, "ObjidTable", "Long"), FakeColumn(128, "Name", "Text", "variable", 255)],
                [{"ObjidTable": 2, "Name": "MSysObjects"}],
            ),
            FakeTable(
                "SruDbIdMapTable",
                31,
                [
                    FakeColumn(1, "IdType", "UnsignedByte", size=1),
                    FakeColumn(2, "IdIndex", "Long"),
                    FakeColumn(256, "IdBlob", "LongBinary", "tagged", None),
                ],
                SRUM_ROWS,
                [FakeIndex("IdIndex", True, True), FakeIndex("IdTypeIdBlob", False, False)],
            ),
            FakeTable("SruDbCheckpointTable", 36, [FakeColumn(1, "ProviderId", "GUID", size=16)], []),
            FakeTable(
                "{973F5D5C-1D90-4944-BE8E-24B94231A174}",
                47,
                [
                    FakeColumn(1, "AutoIncId", "Long"),
                    FakeColumn(2, "TimeStamp", "DateTime", size=8),
                    FakeColumn(3, "AppId", "Long"),
                    FakeColumn(4, "UserId", "Long"),
                    FakeColumn(5, "BytesSent", "LongLong", size=8),
                    FakeColumn(6, "BytesRecvd", "LongLong", size=8),
                ],
                NET_ROWS,
                [FakeIndex("TimeStamp", True, True)],
            ),
        ]

    def tables(self) -> list[FakeTable]:
        return self._tables


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("edb_explorer.core.backends.ese.EseDB", FakeEseDB)


@pytest.fixture
def fake_edb(tmp_path: Path, fake_backend: None) -> Path:
    p = tmp_path / "SRUDB.dat"
    p.write_bytes(b"\x00\x00\x00\x00" + ESE_MAGIC + b"\x00" * 4088)
    return p


@pytest.fixture
def not_edb(tmp_path: Path, fake_backend: None) -> Path:
    p = tmp_path / "random.bin"
    p.write_bytes(b"NOT AN ESE DATABASE" * 10)
    return p


@pytest.fixture
def session(fake_edb: Path) -> Iterator[Session]:
    s = Session()
    s.open(fake_edb)
    yield s
    s.close_all()

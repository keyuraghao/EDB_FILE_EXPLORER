"""Generate a fully synthetic evidence folder for screenshots and demos.  Run: python scripts/make_demo_data.py [dir]

Nothing here comes from a real case: names, domains, phone numbers, SIDs and timestamps are made up
deterministically (seeded), so the output can be published.  Produces:

  <dir>/Browser/History            Chromium history (urls, visits, downloads, keyword_search_terms)
  <dir>/iPhone/sms.db              iOS Messages (message, handle, chat, attachment ...)
  <dir>/Windows/Security.evtx      Windows Security event log (logons, failed logons, privileges, process starts)
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.evtx_writer import build_chunk, build_file

UTC = timezone.utc
T0 = datetime(2025, 3, 3, 8, 0, tzinfo=UTC)
WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

DOMAINS = [
    "docs.example.org",
    "mail.example.net",
    "intranet.example.com",
    "news.example.org",
    "shop.example.net",
    "cloud.example.com",
    "wiki.example.org",
    "forum.example.net",
    "cdn.example.com",
    "status.example.org",
]
PAGES = [
    "index",
    "login",
    "reports/q1",
    "inbox",
    "search",
    "products/1234",
    "wiki/Onboarding",
    "thread/8812",
    "downloads",
    "status",
    "settings",
    "projects/apollo",
    "calendar",
    "profile",
    "help",
]
TITLES = [
    "Home",
    "Sign in",
    "Quarterly report",
    "Inbox (3)",
    "Search results",
    "Product 1234",
    "Onboarding",
    "Thread #8812",
    "Downloads",
    "Service status",
    "Settings",
    "Project Apollo",
    "Calendar",
    "Profile",
    "Help",
]
SEARCHES = [
    "vpn setup",
    "expense policy",
    "python sqlite tutorial",
    "flight to lisbon",
    "printer driver",
    "apollo timeline",
    "meeting notes template",
    "reset password",
    "quarterly targets",
    "team lunch",
]
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Casey", "Riley", "Morgan", "Jamie", "Avery", "Quinn"]
SMS_TEXTS = [
    "On my way",
    "Can you send the report?",
    "Meeting moved to 3pm",
    "Thanks!",
    "Call me when free",
    "Did you see the email?",
    "Lunch tomorrow?",
    "Running late",
    "Sounds good",
    "Sent the files",
    "Where are you?",
    "OK",
    "See you at the office",
    "Happy birthday!",
    "Let's sync on Apollo",
]
PROCS = [
    "C:\\Windows\\System32\\cmd.exe",
    "C:\\Windows\\System32\\svchost.exe",
    "C:\\Program Files\\Demo\\demo.exe",
    "C:\\Windows\\System32\\powershell.exe",
    "C:\\Users\\alex\\Downloads\\setup.exe",
    "C:\\Windows\\explorer.exe",
]
SEC_GUID = "54849625-5478-4994-A5BA-3E3B0328C30D"


def webkit(dt: datetime) -> int:
    return int((dt - WEBKIT_EPOCH).total_seconds() * 1_000_000)


def cocoa_ns(dt: datetime) -> int:
    return int((dt - COCOA_EPOCH).total_seconds() * 1_000_000_000)


def make_history(path: Path, rng: random.Random) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
        CREATE TABLE urls(id INTEGER PRIMARY KEY AUTOINCREMENT, url LONGVARCHAR, title LONGVARCHAR,
            visit_count INTEGER DEFAULT 0 NOT NULL, typed_count INTEGER DEFAULT 0 NOT NULL,
            last_visit_time INTEGER NOT NULL, hidden INTEGER DEFAULT 0 NOT NULL);
        CREATE TABLE visits(id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER NOT NULL, visit_time INTEGER NOT NULL,
            from_visit INTEGER, transition INTEGER DEFAULT 0 NOT NULL, segment_id INTEGER,
            visit_duration INTEGER DEFAULT 0 NOT NULL);
        CREATE TABLE downloads(id INTEGER PRIMARY KEY, guid VARCHAR NOT NULL, current_path LONGVARCHAR NOT NULL,
            target_path LONGVARCHAR NOT NULL, start_time INTEGER NOT NULL, received_bytes INTEGER NOT NULL,
            total_bytes INTEGER NOT NULL, state INTEGER NOT NULL, danger_type INTEGER NOT NULL,
            interrupt_reason INTEGER NOT NULL, end_time INTEGER NOT NULL, opened INTEGER NOT NULL,
            last_access_time INTEGER NOT NULL, referrer VARCHAR NOT NULL, tab_url VARCHAR NOT NULL,
            mime_type VARCHAR(255) NOT NULL);
        CREATE TABLE keyword_search_terms(keyword_id INTEGER NOT NULL, url_id INTEGER NOT NULL,
            term LONGVARCHAR NOT NULL, normalized_term LONGVARCHAR NOT NULL);
        CREATE TABLE visit_source(id INTEGER PRIMARY KEY, source INTEGER NOT NULL);
        """
    )
    con.executemany("INSERT INTO meta VALUES(?, ?)", [("version", "58"), ("last_compatible_version", "16")])
    urls = []
    for i in range(400):
        d, p = rng.choice(DOMAINS), rng.randrange(len(PAGES))
        urls.append((i + 1, f"https://{d}/{PAGES[p]}", TITLES[p], 0, rng.randrange(3), 0, 0))
    con.executemany("INSERT INTO urls VALUES(?,?,?,?,?,?,?)", urls)
    t = T0
    visits = []
    for i in range(1500):
        t += timedelta(minutes=rng.randrange(1, 40))
        uid = rng.randrange(1, 401)
        visits.append(
            (
                i + 1,
                uid,
                webkit(t),
                visits[-1][0] if visits and rng.random() < 0.4 else 0,
                rng.choice([0, 1, 5, 7, 8]),
                None,
                rng.randrange(0, 240_000_000),
            )
        )
    con.executemany("INSERT INTO visits VALUES(?,?,?,?,?,?,?)", visits)
    con.execute("UPDATE urls SET visit_count = (SELECT COUNT(*) FROM visits v WHERE v.url = urls.id)")
    con.execute(
        "UPDATE urls SET last_visit_time = COALESCE((SELECT MAX(visit_time) FROM visits v WHERE v.url = urls.id), ?)",
        (webkit(T0),),
    )
    dls = []
    for i in range(14):
        st = T0 + timedelta(hours=rng.randrange(1, 300))
        name = rng.choice(["report-q1.pdf", "setup.exe", "photos.zip", "invoice-2231.pdf", "notes.docx", "data.csv"])
        size = rng.randrange(20_000, 90_000_000)
        dls.append(
            (
                i + 1,
                f"00000000-0000-4000-8000-{i:012d}",
                f"C:\\Users\\alex\\Downloads\\{name}",
                f"C:\\Users\\alex\\Downloads\\{name}",
                webkit(st),
                size,
                size,
                1,
                0,
                0,
                webkit(st + timedelta(seconds=rng.randrange(2, 400))),
                rng.randrange(2),
                webkit(st),
                f"https://{rng.choice(DOMAINS)}/downloads",
                f"https://{rng.choice(DOMAINS)}/downloads",
                "application/octet-stream",
            )
        )
    con.executemany("INSERT INTO downloads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", dls)
    con.executemany(
        "INSERT INTO keyword_search_terms VALUES(?,?,?,?)",
        [(2, rng.randrange(1, 401), s, s.lower()) for s in SEARCHES],
    )
    con.commit()
    con.close()


def make_sms(path: Path, rng: random.Random) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, country TEXT,
            service TEXT NOT NULL, uncanonicalized_id TEXT, person_centric_id TEXT);
        CREATE TABLE chat(ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT NOT NULL, style INTEGER,
            state INTEGER, chat_identifier TEXT, service_name TEXT, display_name TEXT,
            last_read_message_timestamp INTEGER DEFAULT 0);
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT NOT NULL, text TEXT,
            handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER, date_read INTEGER DEFAULT 0,
            date_delivered INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0,
            cache_has_attachments INTEGER DEFAULT 0);
        CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0);
        CREATE TABLE attachment(ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, created_date INTEGER,
            filename TEXT, mime_type TEXT, transfer_name TEXT, total_bytes INTEGER);
        CREATE TABLE message_attachment_join(message_id INTEGER, attachment_id INTEGER);
        CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
        """
    )
    handles = [(i + 1, f"+1555010{i:02d}", "us", "iMessage" if i % 3 else "SMS", None, None) for i in range(10)]
    con.executemany("INSERT INTO handle VALUES(?,?,?,?,?,?)", handles)
    chats = [
        (i + 1, f"iMessage;-;+1555010{i:02d}", 45, 3, f"+1555010{i:02d}", "iMessage", FIRST[i], 0) for i in range(10)
    ]
    con.executemany("INSERT INTO chat VALUES(?,?,?,?,?,?,?,?)", chats)
    con.executemany("INSERT INTO chat_handle_join VALUES(?,?)", [(i + 1, i + 1) for i in range(10)])
    t = T0
    msgs, joins, atts, ajoins = [], [], [], []
    for i in range(900):
        t += timedelta(minutes=rng.randrange(2, 180))
        h = rng.randrange(1, 11)
        from_me = rng.random() < 0.5
        has_att = rng.random() < 0.05
        msgs.append(
            (
                i + 1,
                f"11111111-0000-4000-8000-{i:012d}",
                rng.choice(SMS_TEXTS),
                h,
                "iMessage" if h % 3 else "SMS",
                cocoa_ns(t),
                cocoa_ns(t + timedelta(minutes=1)) if not from_me else 0,
                cocoa_ns(t + timedelta(seconds=3)) if from_me else 0,
                int(from_me),
                1,
                int(has_att),
            )
        )
        joins.append((h, i + 1, cocoa_ns(t)))
        if has_att:
            aid = len(atts) + 1
            atts.append(
                (
                    aid,
                    f"22222222-0000-4000-8000-{aid:012d}",
                    int((t - COCOA_EPOCH).total_seconds()),
                    f"~/Library/SMS/Attachments/{aid:02x}/IMG_{1000 + aid}.jpeg",
                    "image/jpeg",
                    f"IMG_{1000 + aid}.jpeg",
                    rng.randrange(80_000, 4_000_000),
                )
            )
            ajoins.append((i + 1, aid))
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?,?)", msgs)
    con.executemany("INSERT INTO chat_message_join VALUES(?,?,?)", joins)
    con.executemany("INSERT INTO attachment VALUES(?,?,?,?,?,?,?)", atts)
    con.executemany("INSERT INTO message_attachment_join VALUES(?,?)", ajoins)
    con.commit()
    con.close()


def make_security(path: Path, rng: random.Random) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    users = ["alex", "sam", "jordan", "taylor", "svc_backup", "Administrator"]
    hosts = ["WS-0412", "WS-0413", "LAPTOP-07", "FS-01", "DC-01"]
    t = T0
    recs = []
    for i in range(1, 1201):
        t += timedelta(seconds=rng.randrange(5, 900))
        kind = rng.random()
        base = {"channel": "Security", "provider": "Microsoft-Windows-Security-Auditing", "provider_guid": SEC_GUID}
        if kind < 0.55:
            eid, data = (
                4624,
                {
                    "SubjectUserName": "SYSTEM",
                    "TargetUserName": rng.choice(users),
                    "TargetDomainName": "DEMO",
                    "LogonType": rng.choice([2, 3, 3, 10]),
                    "IpAddress": f"10.20.{rng.randrange(1, 9)}.{rng.randrange(2, 250)}",
                    "WorkstationName": rng.choice(hosts),
                    "LogonProcessName": rng.choice(["User32", "Kerberos", "NtLmSsp"]),
                    "AuthenticationPackageName": rng.choice(["Negotiate", "Kerberos", "NTLM"]),
                },
            )
        elif kind < 0.72:
            eid, data = (
                4625,
                {
                    "SubjectUserName": "-",
                    "TargetUserName": rng.choice([*users, "guest", "root"]),
                    "TargetDomainName": "DEMO",
                    "LogonType": 3,
                    "IpAddress": f"203.0.113.{rng.randrange(2, 250)}",
                    "WorkstationName": rng.choice([*hosts, "UNKNOWN"]),
                    "Status": "0xc000006d",
                    "SubStatus": rng.choice(["0xc0000064", "0xc000006a"]),
                },
            )
        elif kind < 0.82:
            eid, data = (
                4672,
                {
                    "SubjectUserName": rng.choice(["Administrator", "svc_backup"]),
                    "SubjectDomainName": "DEMO",
                    "SubjectLogonId": f"0x{rng.randrange(0x3E7, 0x9FFFF):x}",
                    "PrivilegeList": "SeDebugPrivilege SeBackupPrivilege",
                },
            )
        else:
            eid, data = (
                4688,
                {
                    "SubjectUserName": rng.choice(users),
                    "SubjectDomainName": "DEMO",
                    "NewProcessName": rng.choice(PROCS),
                    "ParentProcessName": "C:\\Windows\\explorer.exe",
                    "TokenElevationType": rng.choice(["%%1936", "%%1938"]),
                    "ProcessId": f"0x{rng.randrange(0x100, 0x9FFF):x}",
                },
            )
        recs.append(
            {"record_id": i, "when": t, "system": {"EventID": eid, "Task": 12544, "UserID": None}, "data": data, **base}
        )
    chunks = [build_chunk(recs[i : i + 20], i) for i in range(0, len(recs), 20)]  # ~2 KB per record, 64 KB chunks
    path.write_bytes(build_file(chunks, next_record_id=len(recs) + 1))


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "demo-evidence"
    rng = random.Random(20260922)
    make_history(out / "Browser" / "History", rng)
    make_sms(out / "iPhone" / "sms.db", rng)
    make_security(out / "Windows" / "Security.evtx", rng)
    print(f"Synthetic evidence written to {out}")


if __name__ == "__main__":
    main()

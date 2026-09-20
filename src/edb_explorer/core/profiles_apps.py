"""Application profiles for non-ESE databases: phones, browsers, macOS, Windows apps, Linux, servers.

Each profile lists the tables that identify it, per-column timestamp encodings
(so the GUI/CLI/MCP show real dates) and *artifact views* - ready-made SQL
over the materialised tables that answer the usual analyst questions.
Table placeholders are written ``{t:TableName}``.
"""

from __future__ import annotations

from edb_explorer.core.profiles import ArtifactView, Profile

V = ArtifactView

# --------------------------------------------------------------------------- #
# Browsers
# --------------------------------------------------------------------------- #
CHROMIUM_HISTORY = Profile(
    id="chromium_history",
    name="Chromium History (Chrome / Edge / Brave / Opera)",
    description="Browsing history, visits, downloads and search terms. Timestamps are WebKit microseconds since 1601.",
    signature_tables=("urls", "visits", "downloads"),
    kinds=("sqlite",),
    platform="browser",
    file_hints=("history",),
    table_names={
        "urls": "urls (visited pages)",
        "visits": "visits (each visit)",
        "downloads": "downloads",
        "keyword_search_terms": "keyword_search_terms (searches)",
        "visit_source": "visit_source",
    },
    column_hints={
        "urls": {"last_visit_time": "webkit"},
        "visits": {"visit_time": "webkit"},
        "downloads": {"start_time": "webkit", "end_time": "webkit", "last_access_time": "webkit"},
        "downloads_url_chains": {},
    },
    views=(
        V(
            "history",
            "Browsing history",
            "Every visit with URL, title and transition, newest first",
            "SELECT v.visit_time AS visited, u.url, u.title, u.visit_count, u.typed_count, v.transition, v.from_visit "
            "FROM {t:visits} v JOIN {t:urls} u ON u.id = v.url ORDER BY v.visit_time DESC",
        ),
        V(
            "downloads",
            "Downloads",
            "Downloaded files with source URL and timing",
            "SELECT start_time, end_time, target_path, tab_url, referrer, received_bytes, total_bytes, state, danger_type, mime_type "
            "FROM {t:downloads} ORDER BY start_time DESC",
        ),
        V(
            "searches",
            "Search terms",
            "Keyword searches typed into the omnibox / search engines",
            "SELECT u.last_visit_time AS last_visited, k.term, u.url FROM {t:keyword_search_terms} k JOIN {t:urls} u ON u.id = k.url_id "
            "ORDER BY u.last_visit_time DESC",
        ),
        V(
            "top_sites",
            "Most visited",
            "URLs ranked by visit count",
            "SELECT url, title, visit_count, typed_count, last_visit_time FROM {t:urls} ORDER BY visit_count DESC LIMIT 500",
        ),
    ),
)

CHROMIUM_COOKIES = Profile(
    id="chromium_cookies",
    name="Chromium Cookies",
    description="Cookie store (values are encrypted with the OS keychain/DPAPI).",
    signature_tables=("cookies",),
    kinds=("sqlite",),
    platform="browser",
    file_hints=("cookies",),
    column_hints={
        "cookies": {
            "creation_utc": "webkit",
            "expires_utc": "webkit",
            "last_access_utc": "webkit",
            "last_update_utc": "webkit",
        }
    },
    views=(
        V(
            "cookies",
            "Cookies",
            "Cookies by host with creation/last access",
            "SELECT host_key, name, path, creation_utc, last_access_utc, expires_utc, is_secure, is_httponly, samesite FROM {t:cookies} ORDER BY last_access_utc DESC",
        ),
    ),
)

CHROMIUM_LOGINS = Profile(
    id="chromium_logins",
    name="Chromium Login Data",
    description="Saved credentials (passwords encrypted) and usage times.",
    signature_tables=("logins",),
    kinds=("sqlite",),
    platform="browser",
    file_hints=("login data",),
    column_hints={
        "logins": {
            "date_created": "webkit",
            "date_last_used": "webkit",
            "date_password_modified": "webkit",
            "date_received": "webkit",
        }
    },
    views=(
        V(
            "logins",
            "Saved logins",
            "Sites with saved credentials",
            "SELECT origin_url, username_value, date_created, date_last_used, date_password_modified, times_used FROM {t:logins} ORDER BY date_last_used DESC",
        ),
    ),
)

CHROMIUM_WEBDATA = Profile(
    id="chromium_webdata",
    name="Chromium Web Data",
    description="Autofill entries, addresses and payment cards.",
    signature_tables=("autofill", "credit_cards"),
    kinds=("sqlite",),
    platform="browser",
    file_hints=("web data",),
    column_hints={
        "autofill": {"date_created": "unix", "date_last_used": "unix"},
        "credit_cards": {"date_modified": "unix", "use_date": "unix"},
        "autofill_profiles": {"date_modified": "unix", "use_date": "unix"},
    },
    views=(
        V(
            "autofill",
            "Autofill",
            "Form field values typed by the user",
            "SELECT name, value, count, date_created, date_last_used FROM {t:autofill} ORDER BY date_last_used DESC",
        ),
    ),
)

FIREFOX_PLACES = Profile(
    id="firefox_places",
    name="Firefox places.sqlite",
    description="Firefox history, bookmarks and downloads. Timestamps are PRTime (Unix microseconds).",
    signature_tables=("moz_places", "moz_historyvisits", "moz_bookmarks"),
    kinds=("sqlite",),
    platform="browser",
    file_hints=("places.sqlite",),
    table_names={
        "moz_places": "moz_places (URLs)",
        "moz_historyvisits": "moz_historyvisits (visits)",
        "moz_bookmarks": "moz_bookmarks",
        "moz_annos": "moz_annos (downloads / annotations)",
        "moz_inputhistory": "moz_inputhistory (typed input)",
    },
    column_hints={
        "moz_places": {"last_visit_date": "unix_us"},
        "moz_historyvisits": {"visit_date": "unix_us"},
        "moz_bookmarks": {"dateAdded": "unix_us", "lastModified": "unix_us"},
        "moz_annos": {"dateAdded": "unix_us", "lastModified": "unix_us"},
        "moz_origins": {},
        "moz_places_metadata": {"created_at": "unix_ms", "updated_at": "unix_ms"},
    },
    views=(
        V(
            "history",
            "Browsing history",
            "Visits joined to URLs",
            "SELECT v.visit_date AS visited, p.url, p.title, p.visit_count, v.visit_type FROM {t:moz_historyvisits} v JOIN {t:moz_places} p ON p.id = v.place_id ORDER BY v.visit_date DESC",
        ),
        V(
            "bookmarks",
            "Bookmarks",
            "Bookmark tree entries with URLs",
            "SELECT b.title, p.url, b.dateAdded, b.lastModified, b.parent FROM {t:moz_bookmarks} b LEFT JOIN {t:moz_places} p ON p.id = b.fk WHERE b.type = 1 ORDER BY b.dateAdded DESC",
        ),
        V(
            "downloads",
            "Downloads",
            "Download annotations (destination file and metadata)",
            "SELECT a.dateAdded, p.url, a.content FROM {t:moz_annos} a JOIN {t:moz_places} p ON p.id = a.place_id ORDER BY a.dateAdded DESC",
        ),
    ),
)

FIREFOX_COOKIES = Profile(
    id="firefox_cookies",
    name="Firefox cookies.sqlite",
    description="Firefox cookie store.",
    signature_tables=("moz_cookies",),
    kinds=("sqlite",),
    platform="browser",
    file_hints=("cookies.sqlite",),
    column_hints={"moz_cookies": {"creationTime": "unix_us", "lastAccessed": "unix_us", "expiry": "unix"}},
    views=(
        V(
            "cookies",
            "Cookies",
            "Cookies by host",
            "SELECT host, name, value, creationTime, lastAccessed, expiry, isSecure, isHttpOnly FROM {t:moz_cookies} ORDER BY lastAccessed DESC",
        ),
    ),
)

FIREFOX_FORMHISTORY = Profile(
    id="firefox_formhistory",
    name="Firefox formhistory.sqlite",
    description="Values typed into web forms.",
    signature_tables=("moz_formhistory",),
    kinds=("sqlite",),
    platform="browser",
    column_hints={"moz_formhistory": {"firstUsed": "unix_us", "lastUsed": "unix_us"}},
    views=(
        V(
            "forms",
            "Form history",
            "Field names and values",
            "SELECT fieldname, value, timesUsed, firstUsed, lastUsed FROM {t:moz_formhistory} ORDER BY lastUsed DESC",
        ),
    ),
)

SAFARI_HISTORY = Profile(
    id="safari_history",
    name="Safari History.db",
    description="Safari browsing history (macOS / iOS). Timestamps are Cocoa seconds since 2001.",
    signature_tables=("history_items", "history_visits"),
    kinds=("sqlite",),
    platform="macos",
    file_hints=("history.db",),
    column_hints={"history_visits": {"visit_time": "cocoa"}, "history_items": {}},
    views=(
        V(
            "history",
            "Browsing history",
            "Visits joined to URLs",
            "SELECT v.visit_time AS visited, i.url, v.title, i.visit_count, v.load_successful FROM {t:history_visits} v JOIN {t:history_items} i ON i.id = v.history_item ORDER BY v.visit_time DESC",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# iOS / macOS
# --------------------------------------------------------------------------- #
IOS_SMS = Profile(
    id="ios_sms",
    name="iOS / macOS Messages (sms.db / chat.db)",
    description="iMessage and SMS conversations. Dates are Cocoa nanoseconds (seconds on older iOS).",
    signature_tables=("message", "handle", "chat"),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("sms.db", "chat.db"),
    table_names={
        "message": "message (all messages)",
        "handle": "handle (phone numbers / Apple IDs)",
        "chat": "chat (conversations)",
        "attachment": "attachment",
    },
    column_hints={
        "message": {
            "date": "cocoa_ns",
            "date_read": "cocoa_ns",
            "date_delivered": "cocoa_ns",
            "date_played": "cocoa_ns",
            "date_edited": "cocoa_ns",
            "date_retracted": "cocoa_ns",
        },
        "attachment": {"created_date": "cocoa"},
        "chat": {"last_read_message_timestamp": "cocoa_ns"},
    },
    views=(
        V(
            "messages",
            "Messages",
            "Every message with sender handle and chat identifier",
            "SELECT m.date AS sent, m.is_from_me, h.id AS handle, c.chat_identifier, m.text, m.service, m.date_read, m.date_delivered, m.ROWID AS message_id "
            "FROM {t:message} m LEFT JOIN {t:handle} h ON h.ROWID = m.handle_id LEFT JOIN {t:chat_message_join} cmj ON cmj.message_id = m.ROWID "
            "LEFT JOIN {t:chat} c ON c.ROWID = cmj.chat_id ORDER BY m.date DESC",
        ),
        V(
            "attachments",
            "Attachments",
            "Files sent or received",
            "SELECT a.created_date, a.filename, a.mime_type, a.total_bytes, a.transfer_name, m.is_from_me FROM {t:attachment} a LEFT JOIN {t:message_attachment_join} j ON j.attachment_id = a.ROWID LEFT JOIN {t:message} m ON m.ROWID = j.message_id ORDER BY a.created_date DESC",
        ),
        V(
            "contacts_activity",
            "Messages per handle",
            "Message counts per correspondent",
            "SELECT h.id AS handle, COUNT(*) AS messages, MIN(m.date) AS first, MAX(m.date) AS last FROM {t:message} m JOIN {t:handle} h ON h.ROWID = m.handle_id GROUP BY h.id ORDER BY messages DESC",
        ),
    ),
)

IOS_ADDRESSBOOK = Profile(
    id="ios_addressbook",
    name="iOS AddressBook.sqlitedb",
    description="Contacts with phone numbers and e-mail addresses.",
    signature_tables=("ABPerson", "ABMultiValue"),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("addressbook",),
    column_hints={"ABPerson": {"CreationDate": "cocoa", "ModificationDate": "cocoa"}},
    views=(
        V(
            "contacts",
            "Contacts",
            "People with all phone/e-mail values",
            "SELECT p.ROWID, p.First, p.Last, p.Organization, mv.value, mv.property, p.CreationDate, p.ModificationDate FROM {t:ABPerson} p LEFT JOIN {t:ABMultiValue} mv ON mv.record_id = p.ROWID ORDER BY p.Last, p.First",
        ),
    ),
)

IOS_CALLS = Profile(
    id="ios_callhistory",
    name="iOS CallHistory.storedata",
    description="Phone / FaceTime call log.",
    signature_tables=("ZCALLRECORD",),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("callhistory",),
    column_hints={"ZCALLRECORD": {"ZDATE": "cocoa"}},
    views=(
        V(
            "calls",
            "Call log",
            "Calls with number, duration and direction (ZORIGINATED 1 = outgoing)",
            "SELECT ZDATE AS date, ZADDRESS AS number, ZDURATION AS seconds, ZORIGINATED AS outgoing, ZANSWERED AS answered, ZCALLTYPE AS call_type, ZSERVICE_PROVIDER FROM {t:ZCALLRECORD} ORDER BY ZDATE DESC",
        ),
    ),
)

KNOWLEDGEC = Profile(
    id="knowledgec",
    name="knowledgeC.db (CoreDuet)",
    description="macOS/iOS app usage, screen state, Bluetooth, media playback, Safari and more (streams in ZSTREAMNAME).",
    signature_tables=("ZOBJECT", "ZSTRUCTUREDMETADATA"),
    kinds=("sqlite",),
    platform="macos",
    file_hints=("knowledgec",),
    column_hints={"ZOBJECT": {"ZSTARTDATE": "cocoa", "ZENDDATE": "cocoa", "ZCREATIONDATE": "cocoa"}},
    views=(
        V(
            "streams",
            "Streams summary",
            "Event counts per stream",
            "SELECT ZSTREAMNAME, COUNT(*) AS events, MIN(ZSTARTDATE) AS first, MAX(ZSTARTDATE) AS last FROM {t:ZOBJECT} GROUP BY ZSTREAMNAME ORDER BY events DESC",
        ),
        V(
            "app_usage",
            "Application usage",
            "/app/usage and /app/inFocus events with bundle id and duration",
            "SELECT ZSTARTDATE AS start, ZENDDATE AS end, ZVALUESTRING AS bundle_id, ZSTREAMNAME AS stream, (ZENDDATE IS NOT NULL) AS has_end FROM {t:ZOBJECT} WHERE ZSTREAMNAME LIKE '/app/%' ORDER BY ZSTARTDATE DESC",
        ),
        V(
            "all_events",
            "All events",
            "Every ZOBJECT row with stream and value",
            "SELECT ZSTARTDATE AS start, ZENDDATE AS end, ZSTREAMNAME AS stream, ZVALUESTRING AS value, ZVALUEINTEGER, ZVALUEDOUBLE FROM {t:ZOBJECT} ORDER BY ZSTARTDATE DESC",
        ),
    ),
)

IOS_PHOTOS = Profile(
    id="apple_photos",
    name="Apple Photos.sqlite",
    description="Photo library metadata (assets, albums, locations).",
    signature_tables=("ZASSET", "ZGENERICASSET"),
    any_of=True,
    kinds=("sqlite",),
    platform="ios",
    file_hints=("photos.sqlite",),
    column_hints={
        "ZASSET": {
            "ZDATECREATED": "cocoa",
            "ZMODIFICATIONDATE": "cocoa",
            "ZADDEDDATE": "cocoa",
            "ZTRASHEDDATE": "cocoa",
        },
        "ZGENERICASSET": {
            "ZDATECREATED": "cocoa",
            "ZMODIFICATIONDATE": "cocoa",
            "ZADDEDDATE": "cocoa",
            "ZTRASHEDDATE": "cocoa",
        },
    },
    views=(
        V(
            "assets",
            "Assets",
            "Photos/videos with capture time, filename and GPS",
            "SELECT ZDATECREATED AS taken, ZADDEDDATE AS added, ZFILENAME, ZDIRECTORY, ZLATITUDE, ZLONGITUDE, ZKIND, ZTRASHEDDATE FROM {t:ZASSET} ORDER BY ZDATECREATED DESC",
        ),
    ),
)

IOS_MANIFEST = Profile(
    id="ios_backup_manifest",
    name="iOS backup Manifest.db",
    description="File list of an iTunes/Finder backup (fileID -> domain/relativePath).",
    signature_tables=("Files", "Properties"),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("manifest.db",),
    views=(
        V(
            "files",
            "Backup files",
            "Every file in the backup with domain and path",
            "SELECT fileID, domain, relativePath, flags FROM {t:Files} ORDER BY domain, relativePath",
        ),
        V(
            "apps",
            "Apps present",
            "Application domains found in the backup",
            "SELECT domain, COUNT(*) AS files FROM {t:Files} WHERE domain LIKE 'AppDomain%' GROUP BY domain ORDER BY files DESC",
        ),
    ),
)

WHATSAPP_IOS = Profile(
    id="whatsapp_ios",
    name="WhatsApp ChatStorage.sqlite (iOS)",
    description="WhatsApp messages, chats and media on iOS.",
    signature_tables=("ZWAMESSAGE", "ZWACHATSESSION"),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("chatstorage",),
    column_hints={
        "ZWAMESSAGE": {"ZMESSAGEDATE": "cocoa", "ZSENTDATE": "cocoa"},
        "ZWACHATSESSION": {"ZLASTMESSAGEDATE": "cocoa"},
        "ZWAMEDIAITEM": {},
    },
    views=(
        V(
            "messages",
            "Messages",
            "Messages with chat partner and direction",
            "SELECT m.ZMESSAGEDATE AS sent, m.ZISFROMME AS from_me, c.ZPARTNERNAME AS chat, m.ZFROMJID AS from_jid, m.ZTEXT AS text, m.ZMESSAGETYPE AS type FROM {t:ZWAMESSAGE} m LEFT JOIN {t:ZWACHATSESSION} c ON c.Z_PK = m.ZCHATSESSION ORDER BY m.ZMESSAGEDATE DESC",
        ),
    ),
)

WHATSAPP_ANDROID = Profile(
    id="whatsapp_android",
    name="WhatsApp msgstore.db (Android)",
    description="WhatsApp messages on Android (legacy 'messages' table or newer 'message' schema).",
    signature_tables=("messages", "chat_list", "message", "chat", "jid"),
    any_of=True,
    kinds=("sqlite",),
    platform="android",
    file_hints=("msgstore",),
    column_hints={
        "messages": {"timestamp": "unix_ms", "received_timestamp": "unix_ms", "send_timestamp": "unix_ms"},
        "message": {"timestamp": "unix_ms", "received_timestamp": "unix_ms"},
        "chat": {"last_message_row_id": ""},
        "call_log": {"timestamp": "unix_ms"},
    },
    views=(
        V(
            "messages_legacy",
            "Messages (legacy schema)",
            "messages table with remote JID",
            "SELECT timestamp AS sent, key_from_me AS from_me, key_remote_jid AS chat, data AS text, media_mime_type, media_name FROM {t:messages} ORDER BY timestamp DESC",
        ),
        V(
            "messages_new",
            "Messages (new schema)",
            "message table joined to chat and jid",
            "SELECT m.timestamp AS sent, m.from_me, j.raw_string AS chat_jid, m.text_data AS text, m.message_type FROM {t:message} m LEFT JOIN {t:chat} c ON c._id = m.chat_row_id LEFT JOIN {t:jid} j ON j._id = c.jid_row_id ORDER BY m.timestamp DESC",
        ),
    ),
)

MAC_QUARANTINE = Profile(
    id="mac_quarantine",
    name="macOS Quarantine events",
    description="Downloaded files recorded by Gatekeeper (LaunchServices QuarantineEventsV2).",
    signature_tables=("LSQuarantineEvent",),
    kinds=("sqlite",),
    platform="macos",
    file_hints=("quarantineevents",),
    column_hints={"LSQuarantineEvent": {"LSQuarantineTimeStamp": "cocoa"}},
    views=(
        V(
            "downloads",
            "Quarantined downloads",
            "Downloaded files with origin URL and agent",
            "SELECT LSQuarantineTimeStamp AS downloaded, LSQuarantineAgentName AS agent, LSQuarantineDataURLString AS url, LSQuarantineOriginURLString AS origin, LSQuarantineTypeNumber FROM {t:LSQuarantineEvent} ORDER BY LSQuarantineTimeStamp DESC",
        ),
    ),
)

MAC_TCC = Profile(
    id="mac_tcc",
    name="macOS TCC.db",
    description="Privacy permissions granted to applications (camera, microphone, full disk access ...).",
    signature_tables=("access", "policies"),
    kinds=("sqlite",),
    platform="macos",
    file_hints=("tcc.db",),
    column_hints={"access": {"last_modified": "unix"}},
    views=(
        V(
            "permissions",
            "Permissions",
            "Service, client and allowed/auth value",
            "SELECT service, client, client_type, auth_value, auth_reason, last_modified FROM {t:access} ORDER BY last_modified DESC",
        ),
    ),
)

MAC_NOTIFICATIONS = Profile(
    id="mac_notifications",
    name="macOS Notification Center db",
    description="Delivered notifications per app.",
    signature_tables=("record", "app"),
    kinds=("sqlite",),
    platform="macos",
    file_hints=("db2/db", "notificationcenter"),
    column_hints={"record": {"delivered_date": "cocoa", "presented": "", "request_date": "cocoa"}},
    views=(
        V(
            "notifications",
            "Notifications",
            "Delivered notifications with app identifier",
            "SELECT r.delivered_date, a.identifier AS app, r.presented, r.style FROM {t:record} r LEFT JOIN {t:app} a ON a.app_id = r.app_id ORDER BY r.delivered_date DESC",
        ),
    ),
)

APPLE_NOTES = Profile(
    id="apple_notes",
    name="Apple Notes NoteStore.sqlite",
    description="Notes app (bodies are gzip-compressed protobuf in ZICNOTEDATA).",
    signature_tables=("ZICCLOUDSYNCINGOBJECT", "ZICNOTEDATA"),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("notestore",),
    column_hints={
        "ZICCLOUDSYNCINGOBJECT": {
            "ZCREATIONDATE": "cocoa",
            "ZMODIFICATIONDATE": "cocoa",
            "ZCREATIONDATE1": "cocoa",
            "ZMODIFICATIONDATE1": "cocoa",
        }
    },
    views=(
        V(
            "notes",
            "Notes",
            "Note titles and snippets with dates",
            "SELECT ZCREATIONDATE1 AS created, ZMODIFICATIONDATE1 AS modified, ZTITLE1 AS title, ZSNIPPET AS snippet, ZFOLDER FROM {t:ZICCLOUDSYNCINGOBJECT} WHERE ZTITLE1 IS NOT NULL ORDER BY ZMODIFICATIONDATE1 DESC",
        ),
    ),
)

APPLE_CALENDAR = Profile(
    id="apple_calendar",
    name="Apple Calendar.sqlitedb",
    description="Calendar events and participants.",
    signature_tables=("CalendarItem", "Calendar"),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("calendar.sqlitedb",),
    column_hints={
        "CalendarItem": {"start_date": "cocoa", "end_date": "cocoa", "creation_date": "cocoa", "last_modified": "cocoa"}
    },
    views=(
        V(
            "events",
            "Events",
            "Calendar items with time span and location",
            "SELECT i.start_date, i.end_date, i.summary, i.location, i.description, c.title AS calendar FROM {t:CalendarItem} i LEFT JOIN {t:Calendar} c ON c.ROWID = i.calendar_id ORDER BY i.start_date DESC",
        ),
    ),
)

IOS_DATAUSAGE = Profile(
    id="ios_datausage",
    name="iOS DataUsage.sqlite",
    description="Per-process cellular/Wi-Fi data usage.",
    signature_tables=("ZLIVEUSAGE", "ZPROCESS"),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("datausage",),
    column_hints={
        "ZLIVEUSAGE": {"ZTIMESTAMP": "cocoa"},
        "ZPROCESS": {"ZFIRSTTIMESTAMP": "cocoa", "ZTIMESTAMP": "cocoa"},
    },
    views=(
        V(
            "usage",
            "Data usage by process",
            "Bytes in/out per process and time",
            "SELECT l.ZTIMESTAMP AS time, p.ZPROCNAME AS process, p.ZBUNDLENAME AS bundle, l.ZWIFIIN, l.ZWIFIOUT, l.ZWWANIN, l.ZWWANOUT FROM {t:ZLIVEUSAGE} l LEFT JOIN {t:ZPROCESS} p ON p.Z_PK = l.ZHASPROCESS ORDER BY l.ZTIMESTAMP DESC",
        ),
    ),
)

INTERACTIONC = Profile(
    id="interactionc",
    name="interactionC.db (CoreDuet interactions)",
    description="Contacts the user interacted with per app (calls, messages, mail).",
    signature_tables=("ZINTERACTIONS", "ZCONTACTS"),
    kinds=("sqlite",),
    platform="ios",
    file_hints=("interactionc",),
    column_hints={
        "ZINTERACTIONS": {"ZSTARTDATE": "cocoa", "ZENDDATE": "cocoa", "ZCREATIONDATE": "cocoa"},
        "ZCONTACTS": {
            "ZFIRSTINCOMINGRECIPIENTDATE": "cocoa",
            "ZLASTINCOMINGRECIPIENTDATE": "cocoa",
            "ZLASTOUTGOINGRECIPIENTDATE": "cocoa",
            "ZCREATIONDATE": "cocoa",
        },
    },
    views=(
        V(
            "interactions",
            "Interactions",
            "Who was contacted through which app",
            "SELECT i.ZSTARTDATE AS start, i.ZBUNDLEID AS app, i.ZDIRECTION AS direction, c.ZDISPLAYNAME AS contact, c.ZIDENTIFIER AS identifier, i.ZMECHANISM FROM {t:ZINTERACTIONS} i LEFT JOIN {t:ZCONTACTS} c ON c.Z_PK = i.ZSENDER ORDER BY i.ZSTARTDATE DESC",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# Android
# --------------------------------------------------------------------------- #
ANDROID_CONTACTS = Profile(
    id="android_contacts",
    name="Android contacts2.db",
    description="Contacts, raw contacts and (on older versions) the call log.",
    signature_tables=("raw_contacts", "data", "contacts"),
    kinds=("sqlite",),
    platform="android",
    file_hints=("contacts2",),
    column_hints={
        "contacts": {"last_time_contacted": "unix_ms"},
        "raw_contacts": {"last_time_contacted": "unix_ms"},
        "calls": {"date": "unix_ms", "last_modified": "unix_ms"},
        "data": {"last_time_used": "unix_ms"},
    },
    views=(
        V(
            "contacts",
            "Contacts",
            "Display names with phone numbers / e-mails from the data table",
            "SELECT r._id AS raw_contact_id, r.display_name, d.mimetype, d.data1 AS value, r.account_name, r.deleted FROM {t:raw_contacts} r LEFT JOIN {t:data} d ON d.raw_contact_id = r._id ORDER BY r.display_name",
        ),
        V(
            "calls",
            "Call log (older Android)",
            "calls table when present in contacts2.db",
            "SELECT date, number, duration, type, name, geocoded_location FROM {t:calls} ORDER BY date DESC",
        ),
    ),
)

ANDROID_CALLLOG = Profile(
    id="android_calllog",
    name="Android calllog.db",
    description="Call log (type 1 incoming, 2 outgoing, 3 missed, 5 rejected).",
    signature_tables=("calls",),
    kinds=("sqlite",),
    platform="android",
    file_hints=("calllog",),
    column_hints={"calls": {"date": "unix_ms", "last_modified": "unix_ms"}},
    views=(
        V(
            "calls",
            "Call log",
            "Calls with number, duration and type",
            "SELECT date, number, duration, type, name, countryiso, geocoded_location, subscription_id FROM {t:calls} ORDER BY date DESC",
        ),
    ),
)

ANDROID_SMS = Profile(
    id="android_sms",
    name="Android mmssms.db",
    description="SMS and MMS messages (sms.type 1 inbox, 2 sent). MMS pdu.date is Unix seconds.",
    signature_tables=("sms", "pdu", "threads"),
    kinds=("sqlite",),
    platform="android",
    file_hints=("mmssms",),
    column_hints={
        "sms": {"date": "unix_ms", "date_sent": "unix_ms"},
        "pdu": {"date": "unix", "date_sent": "unix"},
        "threads": {"date": "unix_ms"},
    },
    views=(
        V(
            "sms",
            "SMS messages",
            "All SMS with address and direction",
            "SELECT date, date_sent, address, type, read, body, thread_id FROM {t:sms} ORDER BY date DESC",
        ),
        V(
            "mms",
            "MMS messages",
            "MMS headers with text parts",
            "SELECT p.date, p.msg_box, p.sub AS subject, a.address, pt.text FROM {t:pdu} p LEFT JOIN {t:addr} a ON a.msg_id = p._id LEFT JOIN {t:part} pt ON pt.mid = p._id AND pt.ct = 'text/plain' ORDER BY p.date DESC",
        ),
    ),
)

ANDROID_DOWNLOADS = Profile(
    id="android_downloads",
    name="Android downloads.db",
    description="DownloadManager records.",
    signature_tables=("downloads",),
    kinds=("sqlite",),
    platform="android",
    file_hints=("downloads.db",),
    column_hints={"downloads": {"lastmod": "unix_ms"}},
    views=(
        V(
            "downloads",
            "Downloads",
            "Downloaded files with URL and status",
            "SELECT lastmod, uri, _data AS path, title, mimetype, total_bytes, status, notificationpackage FROM {t:downloads} ORDER BY lastmod DESC",
        ),
    ),
)

ANDROID_MEDIA = Profile(
    id="android_media",
    name="Android media store (external.db)",
    description="Media scanner index of files on external storage.",
    signature_tables=("files",),
    kinds=("sqlite",),
    platform="android",
    file_hints=("external.db", "internal.db"),
    column_hints={
        "files": {"date_added": "unix", "date_modified": "unix", "datetaken": "unix_ms", "date_expires": "unix"}
    },
    views=(
        V(
            "files",
            "Files",
            "Indexed files with dates and size",
            "SELECT date_added, date_modified, datetaken, _data AS path, _size AS size, mime_type, owner_package_name FROM {t:files} ORDER BY date_modified DESC",
        ),
    ),
)

ANDROID_ACCOUNTS = Profile(
    id="android_accounts",
    name="Android accounts.db",
    description="Accounts registered on the device.",
    signature_tables=("accounts", "authtokens"),
    kinds=("sqlite",),
    platform="android",
    file_hints=("accounts",),
    column_hints={"accounts": {"last_password_entry_time_millis_epoch": "unix_ms"}},
    views=(
        V(
            "accounts",
            "Accounts",
            "Account names and types",
            "SELECT name, type, last_password_entry_time_millis_epoch FROM {t:accounts}",
        ),
    ),
)

TELEGRAM_ANDROID = Profile(
    id="telegram_android",
    name="Telegram cache4.db (Android)",
    description="Telegram message cache (message bodies are TL-serialised blobs).",
    signature_tables=("messages", "users", "dialogs"),
    kinds=("sqlite",),
    platform="android",
    file_hints=("cache4",),
    column_hints={"messages": {"date": "unix"}, "dialogs": {"date": "unix"}, "users": {}},
    views=(
        V(
            "messages",
            "Messages",
            "Message timestamps per dialog",
            "SELECT date, uid AS dialog, mid, out, read_state, send_state FROM {t:messages} ORDER BY date DESC",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# Windows applications
# --------------------------------------------------------------------------- #
WIN_ACTIVITIES = Profile(
    id="windows_activitiescache",
    name="Windows Timeline (ActivitiesCache.db)",
    description="Windows 10 Timeline: applications used, documents opened, clipboard, per-session focus. Unix seconds.",
    signature_tables=("Activity", "ActivityOperation", "Activity_PackageId"),
    kinds=("sqlite",),
    platform="windows",
    file_hints=("activitiescache",),
    column_hints={
        "Activity": {
            "StartTime": "unix",
            "EndTime": "unix",
            "LastModifiedTime": "unix",
            "ExpirationTime": "unix",
            "CreatedInCloud": "unix",
            "LastModifiedOnClient": "unix",
            "OriginalLastModifiedOnClient": "unix",
        },
        "ActivityOperation": {
            "StartTime": "unix",
            "EndTime": "unix",
            "LastModifiedTime": "unix",
            "ExpirationTime": "unix",
            "CreatedTime": "unix",
            "OperationExpirationTime": "unix",
        },
        "Activity_PackageId": {"ExpirationTime": "unix"},
    },
    views=(
        V(
            "activities",
            "Activities",
            "Application activity with start/end and payload",
            "SELECT StartTime, EndTime, LastModifiedTime, AppId, ActivityType, Payload, ClipboardPayload FROM {t:Activity} ORDER BY StartTime DESC",
        ),
        V(
            "apps",
            "Applications",
            "Package identifiers seen",
            "SELECT ActivityId, Platform, PackageName, ExpirationTime FROM {t:Activity_PackageId} ORDER BY ExpirationTime DESC",
        ),
    ),
)

WIN_NOTIFICATIONS = Profile(
    id="windows_notifications",
    name="Windows Notifications (wpndatabase.db)",
    description="Toast/tile notifications received by the user. FILETIME timestamps.",
    signature_tables=("Notification", "NotificationHandler"),
    kinds=("sqlite",),
    platform="windows",
    file_hints=("wpndatabase",),
    column_hints={"Notification": {"ArrivalTime": "filetime", "ExpiryTime": "filetime"}},
    views=(
        V(
            "notifications",
            "Notifications",
            "Notification payload with handler (app)",
            "SELECT n.ArrivalTime, n.ExpiryTime, h.PrimaryId AS app, n.Type, n.Payload FROM {t:Notification} n LEFT JOIN {t:NotificationHandler} h ON h.RecordId = n.HandlerId ORDER BY n.ArrivalTime DESC",
        ),
    ),
)

SKYPE_MAIN = Profile(
    id="skype_main",
    name="Skype main.db",
    description="Classic Skype messages, calls and contacts.",
    signature_tables=("Messages", "Contacts", "Conversations"),
    kinds=("sqlite",),
    platform="cross",
    file_hints=("main.db",),
    column_hints={
        "Messages": {"timestamp": "unix", "timestamp__ms": "unix_ms"},
        "Calls": {"begin_timestamp": "unix"},
        "Contacts": {"lastonline_timestamp": "unix"},
    },
    views=(
        V(
            "messages",
            "Messages",
            "Chat messages with author",
            "SELECT timestamp, author, from_dispname, dialog_partner, body_xml, chatname FROM {t:Messages} ORDER BY timestamp DESC",
        ),
    ),
)

THUNDERBIRD = Profile(
    id="thunderbird_gloda",
    name="Thunderbird global-messages-db.sqlite",
    description="Thunderbird message index (Gloda).",
    signature_tables=("messages", "messagesText", "conversations"),
    kinds=("sqlite",),
    platform="linux",
    file_hints=("global-messages-db",),
    column_hints={"messages": {"date": "unix_us"}},
    views=(
        V(
            "messages",
            "Messages",
            "Indexed mails with subject and sender",
            "SELECT m.date, t.c1author AS author, t.c0body AS body_snippet, c.subject FROM {t:messages} m LEFT JOIN {t:messagesText} t ON t.docid = m.id LEFT JOIN {t:conversations} c ON c.id = m.conversationID ORDER BY m.date DESC",
        ),
    ),
)

ZEITGEIST = Profile(
    id="zeitgeist",
    name="GNOME Zeitgeist activity.sqlite",
    description="Linux desktop activity log (files opened, apps used).",
    signature_tables=("event", "uri", "actor"),
    kinds=("sqlite",),
    platform="linux",
    file_hints=("activity.sqlite",),
    column_hints={"event": {"timestamp": "unix_ms"}},
    views=(
        V(
            "events",
            "Events",
            "Events with subject URI and actor",
            "SELECT e.timestamp, u.value AS uri, a.value AS actor FROM {t:event} e LEFT JOIN {t:uri} u ON u.id = e.subj_id LEFT JOIN {t:actor} a ON a.id = e.actor ORDER BY e.timestamp DESC",
        ),
    ),
)

SIGNAL_DESKTOP = Profile(
    id="signal_desktop",
    name="Signal Desktop db.sqlite",
    description="Signal Desktop (SQLCipher-encrypted unless already decrypted).",
    signature_tables=("messages", "conversations", "sessions"),
    kinds=("sqlite",),
    platform="cross",
    column_hints={
        "messages": {"sent_at": "unix_ms", "received_at": "unix_ms", "expires_at": "unix_ms"},
        "conversations": {"active_at": "unix_ms"},
    },
    views=(
        V(
            "messages",
            "Messages",
            "Message bodies per conversation",
            "SELECT m.sent_at, m.type, c.name AS conversation, m.body, m.hasAttachments FROM {t:messages} m LEFT JOIN {t:conversations} c ON c.id = m.conversationId ORDER BY m.sent_at DESC",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# LevelDB (directory-based) - detection uses the directory path
# --------------------------------------------------------------------------- #
CHROMIUM_LOCALSTORAGE = Profile(
    id="chromium_localstorage",
    name="Chromium Local Storage (LevelDB)",
    description="window.localStorage of every origin: keys are '_origin\\x00\\x01name', values UTF-16/Latin-1.",
    signature_tables=("live",),
    kinds=("leveldb",),
    platform="browser",
    file_hints=("local storage",),
    any_of=True,
    views=(
        V(
            "storage",
            "Local storage entries",
            "Live key/value pairs decoded",
            "SELECT key_text, value_text FROM {t:live} ORDER BY key_text",
        ),
        V(
            "history",
            "All versions incl. deleted",
            "Every put/delete across log and table files",
            "SELECT sequence, operation, key_text, value_text, file FROM {t:all_records} ORDER BY sequence DESC",
        ),
    ),
)
CHROMIUM_INDEXEDDB = Profile(
    id="chromium_indexeddb",
    name="Chromium IndexedDB (LevelDB)",
    description="IndexedDB store of a web app / Electron app (Teams, Slack, Discord, WhatsApp Web ...). Values are V8-serialised objects.",
    signature_tables=("live",),
    kinds=("leveldb",),
    platform="browser",
    file_hints=("indexeddb", ".leveldb"),
    any_of=True,
    views=(V("records", "Live records", "Latest value per key", "SELECT key_hex, key_text, value_text FROM {t:live}"),),
)

# --------------------------------------------------------------------------- #
# Servers / dumps / misc
# --------------------------------------------------------------------------- #
WORDPRESS = Profile(
    id="wordpress",
    name="WordPress database",
    description="WordPress site database (users, posts, comments, options).",
    signature_tables=("wp_users", "wp_posts", "wp_options"),
    kinds=("sqldump", "sqlite"),
    platform="server",
    views=(
        V(
            "users",
            "Users",
            "Accounts with registration date and e-mail",
            "SELECT ID, user_login, user_email, user_registered, display_name FROM {t:wp_users} ORDER BY user_registered DESC",
        ),
        V(
            "posts",
            "Posts",
            "Published content",
            "SELECT ID, post_date, post_author, post_title, post_status, post_type, guid FROM {t:wp_posts} ORDER BY post_date DESC",
        ),
        V(
            "comments",
            "Comments",
            "Comments with author IP",
            "SELECT comment_ID, comment_date, comment_author, comment_author_email, comment_author_IP, comment_approved, comment_content FROM {t:wp_comments} ORDER BY comment_date DESC",
        ),
    ),
)
RPM_PACKAGES = Profile(
    id="rpm_packages",
    name="RPM database (Packages)",
    description="Installed package headers (Berkeley DB, pre-RPM 4.16).",
    signature_tables=("records",),
    kinds=("bsddb",),
    platform="linux",
    file_hints=("packages",),
    any_of=True,
)
MOZILLA_CERT8 = Profile(
    id="mozilla_cert8",
    name="Mozilla cert8.db / key3.db",
    description="Legacy NSS certificate / key store (Berkeley DB).",
    signature_tables=("records",),
    kinds=("bsddb",),
    platform="browser",
    file_hints=("cert8", "key3", "secmod"),
    any_of=True,
)
MONGO_DUMP = Profile(
    id="mongo_dump",
    name="MongoDB collection dump",
    description="A mongodump .bson collection.",
    signature_tables=(),
    kinds=("bson",),
    platform="server",
)

APP_PROFILES: tuple[Profile, ...] = (
    CHROMIUM_HISTORY,
    CHROMIUM_COOKIES,
    CHROMIUM_LOGINS,
    CHROMIUM_WEBDATA,
    FIREFOX_PLACES,
    FIREFOX_COOKIES,
    FIREFOX_FORMHISTORY,
    SAFARI_HISTORY,
    IOS_SMS,
    IOS_ADDRESSBOOK,
    IOS_CALLS,
    KNOWLEDGEC,
    IOS_PHOTOS,
    IOS_MANIFEST,
    WHATSAPP_IOS,
    WHATSAPP_ANDROID,
    MAC_QUARANTINE,
    MAC_TCC,
    MAC_NOTIFICATIONS,
    APPLE_NOTES,
    APPLE_CALENDAR,
    IOS_DATAUSAGE,
    INTERACTIONC,
    ANDROID_CONTACTS,
    ANDROID_CALLLOG,
    ANDROID_SMS,
    ANDROID_DOWNLOADS,
    ANDROID_MEDIA,
    ANDROID_ACCOUNTS,
    TELEGRAM_ANDROID,
    WIN_ACTIVITIES,
    WIN_NOTIFICATIONS,
    SKYPE_MAIN,
    THUNDERBIRD,
    ZEITGEIST,
    SIGNAL_DESKTOP,
    CHROMIUM_LOCALSTORAGE,
    CHROMIUM_INDEXEDDB,
    WORDPRESS,
    RPM_PACKAGES,
    MOZILLA_CERT8,
    MONGO_DUMP,
)

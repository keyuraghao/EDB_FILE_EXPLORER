"""Known-database profiles.

ESE is a generic storage engine used by many Windows components.  Knowing
*which* application produced a file lets us show friendly table names and
descriptions (SRUM tables are named by GUID, for example) and lets AI agents
reason about what they are looking at.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str
    description: str
    signature_tables: tuple[str, ...]
    table_names: dict[str, str] = field(default_factory=dict)
    table_descriptions: dict[str, str] = field(default_factory=dict)
    file_hints: tuple[str, ...] = ()

    def display_name(self, table: str) -> str:
        return self.table_names.get(table, table)

    def describe(self, table: str) -> str | None:
        return self.table_descriptions.get(table)


_SRUM_TABLES = {
    "{D10CA2FE-6FCF-4F6D-848E-B2E99266FA89}": "Application Resource Usage",
    "{973F5D5C-1D90-4944-BE8E-24B94231A174}": "Network Data Usage",
    "{DD6636C4-8929-4683-974E-22C046A43763}": "Network Connectivity Usage",
    "{FEE4E14F-02A9-4550-B5CE-5FA2DA202E37}": "Energy Usage",
    "{FEE4E14F-02A9-4550-B5CE-5FA2DA202E37}LT": "Energy Usage (Long Term)",
    "{D10CA2FE-6FCF-4F6D-848E-B2E99266FA86}": "Push Notifications",
    "{5C8CF1C7-7257-4F13-B223-970EF5939312}": "App Timeline Provider",
    "{7ACBBAA3-D029-4BE4-9A7A-0885927F1D8F}": "vfuprov (VFU Provider)",
    "{DA73FB89-2BEA-4DDC-86B8-6E048C6DA477}": "SDP Volume Provider",
    "{841A7317-3805-518B-C2EA-AD224CB4AF84}": "SDP Physical Disk Provider",
    "{DC3D3B50-BB90-5066-FA4E-A5F90DD8B677}": "SDP CPU Provider",
    "{17F4D97B-F26A-5E79-3A82-90040A47D13D}": "SDP Network Provider",
    "{B6D82AF1-F780-4E17-8077-6CB9AD8A6FC4}": "Tagged Energy Provider",
    "{6E3D8DE6-5DA5-4F6C-9A4F-9B4A2A5C1A5B}": "SDP Event Log Provider",
    "SruDbIdMapTable": "SRUM ID Map (apps / users)",
    "SruDbCheckpointTable": "SRUM Checkpoints",
}

_SRUM_DESCRIPTIONS = {
    "{D10CA2FE-6FCF-4F6D-848E-B2E99266FA89}": "Per-app CPU, I/O and foreground/background usage sampled hourly.",
    "{973F5D5C-1D90-4944-BE8E-24B94231A174}": "Per-app bytes sent/received per network interface.",
    "{DD6636C4-8929-4683-974E-22C046A43763}": "Network connect/disconnect events with profile IDs.",
    "SruDbIdMapTable": "Maps AppId/UserId integers to application paths and user SIDs (IdBlob is UTF-16 or SID).",
}

PROFILES: tuple[Profile, ...] = (
    Profile(
        id="ntds",
        name="Active Directory (NTDS.dit)",
        description="Active Directory domain database: objects, attributes, security descriptors and links.",
        signature_tables=("datatable", "link_table", "sd_table"),
        table_names={
            "datatable": "datatable (AD objects)",
            "link_table": "link_table (group membership / linked attrs)",
            "sd_table": "sd_table (security descriptors)",
            "hiddentable": "hiddentable (schema / DSA state)",
        },
        table_descriptions={
            "datatable": "One row per directory object. Columns are ATTk<attributeID>; "
            "ATTm3 = cn, ATTm590045 = sAMAccountName, ATTk589914 = unicodePwd (encrypted).",
            "link_table": "Linked attribute values (e.g. member/memberOf) keyed by DNT.",
            "sd_table": "Security descriptors referenced by nTSecurityDescriptor.",
        },
        file_hints=("ntds.dit",),
    ),
    Profile(
        id="srum",
        name="System Resource Usage Monitor (SRUDB.dat)",
        description="Windows SRUM telemetry: per-application network, CPU, energy and connectivity usage.",
        signature_tables=("SruDbIdMapTable", "SruDbCheckpointTable"),
        table_names=_SRUM_TABLES,
        table_descriptions=_SRUM_DESCRIPTIONS,
        file_hints=("srudb.dat",),
    ),
    Profile(
        id="exchange",
        name="Exchange Mailbox Database",
        description="Microsoft Exchange Server mailbox store (folders, messages, attachments per mailbox).",
        signature_tables=("Mailbox", "Folder", "Message"),
        table_names={
            "Mailbox": "Mailbox (mailbox table)",
            "Folder": "Folder (folder hierarchy)",
            "Message": "Message (message headers)",
            "Attachment": "Attachment",
            "Globals": "Globals (store-wide properties)",
        },
        file_hints=("mailbox database", ".edb"),
    ),
    Profile(
        id="webcache",
        name="IE / Edge WebCache (WebCacheV01.dat)",
        description="Internet Explorer / legacy Edge browsing history, cache, cookies and downloads.",
        signature_tables=("Containers", "LeakFiles"),
        table_names={
            "Containers": "Containers (index of Container_N tables)",
            "LeakFiles": "LeakFiles",
            "Partitions": "Partitions",
        },
        table_descriptions={
            "Containers": "Each row names a Container_<id> table and its purpose "
            "(History, Content, Cookies, iedownload...).",
        },
        file_hints=("webcachev01.dat", "webcachev24.dat"),
    ),
    Profile(
        id="windows_search",
        name="Windows Search Index (Windows.edb)",
        description="Windows Search indexer: indexed file metadata, paths and content snippets.",
        signature_tables=("SystemIndex_PropertyStore", "SystemIndex_Gthr"),
        table_names={
            "SystemIndex_PropertyStore": "SystemIndex_PropertyStore (file properties)",
            "SystemIndex_Gthr": "SystemIndex_Gthr (gatherer: crawl state per item)",
            "SystemIndex_GthrPth": "SystemIndex_GthrPth (path hierarchy)",
        },
        file_hints=("windows.edb",),
    ),
    Profile(
        id="ual",
        name="User Access Logging (UAL)",
        description="Windows Server User Access Logging: role usage per client IP/user.",
        signature_tables=("CLIENTS", "ROLE_ACCESS"),
        table_names={
            "CLIENTS": "CLIENTS (per-client access records)",
            "ROLE_ACCESS": "ROLE_ACCESS (role definitions)",
            "DNS": "DNS (hostname resolution)",
            "VIRTUALMACHINES": "VIRTUALMACHINES",
            "SYSTEMIDENTITY": "SYSTEMIDENTITY",
            "CHAINED_DATABASES": "CHAINED_DATABASES",
        },
        file_hints=("current.mdb", "systemidentity.mdb"),
    ),
    Profile(
        id="windows_update",
        name="Windows Update DataStore (DataStore.edb)",
        description="Windows Update history, files and update metadata.",
        signature_tables=("tbFiles", "tbUpdates"),
        table_names={"tbHistory": "tbHistory (install history)", "tbUpdates": "tbUpdates"},
        file_hints=("datastore.edb",),
    ),
    Profile(
        id="windows_mail",
        name="Windows Mail / Contacts Store",
        description="Windows Mail app store (messages, contacts, appointments).",
        signature_tables=("Message", "Contact", "Folders"),
        file_hints=("store.vol",),
    ),
    Profile(
        id="spartan",
        name="Edge (Spartan) Favorites/Reading List",
        description="Legacy Microsoft Edge (Spartan) favorites and reading list store.",
        signature_tables=("Favorites", "ReadingList"),
        file_hints=("spartan.edb",),
    ),
    Profile(
        id="catdb",
        name="Catalog Database (catdb)",
        description="Windows CryptoAPI catalog database (signed file hashes).",
        signature_tables=("HashCatNameTable", "CatalogEntries"),
        file_hints=("catdb",),
    ),
)

GENERIC = Profile(
    id="generic",
    name="Generic ESE database",
    description="An Extensible Storage Engine database of unknown application type.",
    signature_tables=(),
)

SYSTEM_TABLES = frozenset(
    {"MSysObjects", "MSysObjectsShadow", "MSysObjids", "MSysLocales", "MSysDefrag2", "MSysUnicodeFixupVer2"}
)


def detect_profile(table_names: list[str] | set[str], file_name: str | None = None) -> Profile:
    """Pick the best profile for a database based on its table names (and file name as a tiebreak)."""
    names = set(table_names)
    lowered = (file_name or "").lower()
    best: Profile | None = None
    best_score = 0
    for profile in PROFILES:
        if not profile.signature_tables:
            continue
        matched = sum(1 for t in profile.signature_tables if t in names)
        if matched == 0:
            continue
        score = matched * 10 + (5 if any(h in lowered for h in profile.file_hints) else 0)
        if matched == len(profile.signature_tables):
            score += 100
        if score > best_score:
            best, best_score = profile, score
    return best or GENERIC


def profile_by_id(profile_id: str) -> Profile:
    for p in PROFILES:
        if p.id == profile_id:
            return p
    return GENERIC


def is_system_table(name: str) -> bool:
    return name in SYSTEM_TABLES or name.startswith("MSys")

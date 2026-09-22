# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: builds one folder containing

  * EDB-Explorer      - windowed GUI executable
  * edb-explorer      - console executable (CLI + MCP server)

Both share the same bundled libraries.  Run from the repo root:

    pyinstaller packaging/edb-explorer.spec --noconfirm
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is injected by PyInstaller
SRC = ROOT / "src"
RES = SRC / "edb_explorer" / "resources"
sys.path.insert(0, str(SRC))

from edb_explorer import __version__  # noqa: E402

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
ICON = str(RES / ("icon.ico" if IS_WINDOWS else "icon.png")) if (IS_WINDOWS or IS_MAC) else None

hiddenimports = (
    collect_submodules("dissect.esedb")
    + collect_submodules("dissect.cstruct")
    + collect_submodules("dissect.util")
    + collect_submodules("dissect.database")
    + collect_submodules("access_parser")
    + collect_submodules("dbfread")
    + collect_submodules("bson")
    + collect_submodules("evtx")
    + ["cramjam", "construct", "tabulate", "pyte", "wcwidth"]
    + (["winpty"] if sys.platform.startswith("win") else [])
    + collect_submodules("edb_explorer")
    + collect_submodules("mcp")
    + collect_submodules("mcp_types")
    + collect_submodules("openpyxl")
    + collect_submodules("reportlab")
    + collect_submodules("docx")
    + collect_submodules("cryptography")
    + ["typer", "rich", "anyio", "pydantic", "starlette", "uvicorn"]
)
# The evtx parser is a compiled Rust extension (evtx/_native*.so|pyd); bundle its shared library explicitly.
binaries = collect_dynamic_libs("evtx")
datas = (
    [(str(RES), "edb_explorer/resources")]
    + collect_data_files("docx")
    + collect_data_files("reportlab", includes=["fonts/*"])
    + collect_data_files("mcp")
    + collect_data_files("wcwidth")
)
excludes = [
    "tkinter", "matplotlib", "numpy", "scipy", "pandas", "IPython", "jupyter", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtWebSockets", "PySide6.QtWebChannel",
]

gui_a = Analysis(
    [str(SRC / "edb_explorer" / "gui" / "app.py")],
    pathex=[str(SRC)],
    hiddenimports=hiddenimports,
    binaries=binaries,
    datas=datas,
    excludes=excludes,
    noarchive=False,
)
cli_a = Analysis(
    [str(SRC / "edb_explorer" / "__main__.py")],
    pathex=[str(SRC)],
    hiddenimports=hiddenimports,
    binaries=binaries,
    datas=datas,
    excludes=excludes,
    noarchive=False,
)

# Drop Qt modules the app never loads (QML/Quick, PDF, virtual keyboard, translations) to keep the bundle small.
_PRUNE = ("Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6VirtualKeyboard", "Qt6Labs", "Qt6Multimedia", "Qt6WebEngine",
          "Qt6Charts", "Qt6DataVisualization", "Qt6Positioning", "Qt6Location", "Qt6Sensors", "Qt6SerialPort",
          "Qt6Bluetooth", "Qt6Nfc", "Qt6RemoteObjects", "Qt6Scxml", "Qt6TextToSpeech", "Qt6Test", "Qt6Designer",
          "Qt6Help", "Qt6ShaderTools", "Qt6SpatialAudio", "Qt6Graphs", "Qt6HttpServer", "Qt6WebSockets",
          "Qt6WebChannel", "Qt6WebView", "Qt6StateMachine", "Qt6Sql", "Qt63D", "Qt6Concurrent", "Qt6PrintSupport",
          "Qt6OpenGLWidgets", "Qt6Xml", "Qt6Svg", "Qt6UiTools",
          "/translations/", "\\translations\\", "qml/", "qml\\", "plugins/qmltooling", "plugins/sceneparsers",
          "plugins/geometryloaders", "plugins/renderers", "plugins/sqldrivers", "plugins/multimedia",
          "plugins/webview", "plugins/designer", "plugins/position", "plugins/sensors", "plugins/canbus",
          "plugins/texttospeech", "plugins/printsupport")


def _keep(entry):
    name = entry[0].replace("\\", "/")
    dest = entry[1].replace("\\", "/") if isinstance(entry[1], str) else ""
    text = f"{name} {dest}"
    return not any(p.replace("\\", "/") in text for p in _PRUNE)


for a in (gui_a, cli_a):
    a.binaries = TOC([e for e in a.binaries if _keep(e)])  # noqa: F821 - TOC is injected by PyInstaller
    a.datas = TOC([e for e in a.datas if _keep(e)])  # noqa: F821

MERGE((gui_a, "EDB-Explorer", "EDB-Explorer"), (cli_a, "edb-explorer", "edb-explorer"))

gui_pyz = PYZ(gui_a.pure)
cli_pyz = PYZ(cli_a.pure)

gui_exe = EXE(
    gui_pyz, gui_a.scripts, [],
    exclude_binaries=True, name="EDB-Explorer", debug=False, strip=False, upx=False,
    console=False, icon=ICON, version=None,
)
cli_exe = EXE(
    cli_pyz, cli_a.scripts, [],
    exclude_binaries=True, name="edb-explorer", debug=False, strip=False, upx=False,
    console=True, icon=ICON,
)
coll = COLLECT(
    gui_exe, gui_a.binaries, gui_a.datas,
    cli_exe, cli_a.binaries, cli_a.datas,
    strip=False, upx=False, name=f"edb-explorer-{__version__}",
)
if IS_MAC:
    app = BUNDLE(coll, name="EDB Explorer.app", icon=str(RES / "icon.png"), bundle_identifier="io.github.edbexplorer",
                 info_plist={"CFBundleShortVersionString": __version__, "NSHighResolutionCapable": True})

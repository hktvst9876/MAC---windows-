# PyInstaller spec for MirrorBridge (Windows .exe folder / macOS .app bundle).
#   pyinstaller --noconfirm packaging/mirrorbridge.spec
# Run tools/make_icons.py first (and iconutil on macOS) to create the icons.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
ICONS = ROOT / "packaging" / "icons"
VERSION = "0.1.0"

if sys.platform == "win32":
    icon = ICONS / "MirrorBridge.ico"
elif sys.platform == "darwin":
    icon = ICONS / "MirrorBridge.icns"
else:
    icon = ICONS / "MirrorBridge.png"
icon = str(icon) if icon.exists() else None

hiddenimports = collect_submodules("zeroconf") + collect_submodules("mirrorbridge")

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPdf", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtMultimedia", "pytest", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MirrorBridge",
    console=False,
    icon=icon,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="MirrorBridge", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MirrorBridge.app",
        icon=icon,
        bundle_identifier="org.mirrorbridge.receiver",
        version=VERSION,
        info_plist={
            "CFBundleName": "MirrorBridge",
            "CFBundleDisplayName": "MirrorBridge",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSApplicationCategoryType": "public.app-category.utilities",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription":
                "MirrorBridge records a voice-over from the microphone when you enable it for recordings.",
            "NSLocalNetworkUsageDescription":
                "MirrorBridge receives screen mirroring from iPhone, iPad and Mac on your local network.",
            "NSBonjourServices": ["_airplay._tcp", "_raop._tcp"],
        },
    )

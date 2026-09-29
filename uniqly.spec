# -*- mode: python ; coding: utf-8 -*-
"""Сборка Uniqly в один .app (macOS) / .exe (Windows).

    venv311/bin/pyinstaller uniqly.spec --noconfirm

Что важно:
- ffmpeg/ffprobe кладутся ВНУТРЬ программы (bin/<платформа>/) — у пользователя
  своего ffmpeg может не быть. Пути к ним резолвит app/binaries.py.
- лишние модули Qt выброшены (excludes): без этого .app весит сотни мегабайт,
  а реально используются только 5 модулей.
- личные файлы (бот, вотермарк, .env, workspace) в сборку
  НЕ попадают — только код приложения.
"""
import sys
from pathlib import Path

ROOT = Path(SPECPATH)

# --- бинарники ffmpeg/ffprobe под текущую платформу ---
if sys.platform == "darwin":
    import platform
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    BIN_DIR = f"bin/macos-{arch}"
    BIN_FILES = ["ffmpeg", "ffprobe"]
elif sys.platform.startswith("win"):
    BIN_DIR = "bin/windows-x64"
    BIN_FILES = ["ffmpeg.exe", "ffprobe.exe"]
else:
    BIN_DIR = "bin/linux-x64"
    BIN_FILES = ["ffmpeg", "ffprobe"]

datas = []
for name in BIN_FILES:
    src = ROOT / BIN_DIR / name
    if src.exists():
        datas.append((str(src), BIN_DIR))
    else:
        raise SystemExit(
            f"нет {src} — сначала скачай статический ffmpeg:\n"
            f"    ./fetch_ffmpeg.sh"
        )

# шрифт надписи для пресета tiktok (см. processors/caption.py, config.resource_path)
font = ROOT / "fonts" / "SourceSans3.ttf"
if font.exists():
    datas.append((str(font), "fonts"))

# Модули Qt, которые приложение НЕ использует. Без этого списка PyInstaller
# тащит весь PySide6 (сотни МБ): 3D, WebEngine, Charts, Designer и прочее.
# Используются ровно пять: QtCore, QtGui, QtWidgets, QtMultimedia,
# QtMultimediaWidgets — они и остаются.
QT_EXCLUDES = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtNfc",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtPositioning", "PySide6.QtQml",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtSerialBus", "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio", "PySide6.QtSql", "PySide6.QtStateMachine",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtTest",
    "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets", "PySide6.QtXml",
]

EXCLUDES = QT_EXCLUDES + [
    # тяжёлые библиотеки, которые тянутся транзитивно и не нужны
    "tkinter", "matplotlib", "numpy", "scipy", "pandas", "PIL.ImageQt",
    "pytest", "setuptools", "pip",
    # телеграм-бот — отдельный проект, в приложение не входит
    "telegram",
    # psycopg2 НЕ выбрасываем: через него работают личные вкладки
    # («Посты TG», «Статистика» ходят в базу бота в Neon). У друга их нет,
    # но библиотека весит мало, а без неё личная сборка падает на старте.
]

a = Analysis(
    ["app/main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # пресеты импортируются по имени через channels.PRESETS —
        # статический анализ их не видит
        "processors.reels", "processors.crop", "processors.tiktok",
        "processors.caption",
        # ключи для друзей (файл может отсутствовать — тогда сборка
        # получится без них, поиск фильма попросит ключ в настройках)
        "bundled_env",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Uniqly",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX ломает подпись на macOS
    console=False,      # без окна терминала
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Uniqly",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Uniqly.app",
        icon=None,
        bundle_identifier="com.uniqly.app",
        info_plist={
            "CFBundleName": "Uniqly",
            "CFBundleDisplayName": "Uniqly",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            # приложение лезет в сеть (скачивание роликов, КиноПоиск)
            "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True},
        },
    )

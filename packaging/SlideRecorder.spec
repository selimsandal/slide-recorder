# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

project_root = Path(SPECPATH).parent.parent

a = Analysis(
    [str(project_root / "packaging" / "pyinstaller_entry.py")],
    pathex=[str(project_root / "src")],
    binaries=collect_dynamic_libs("pymupdf"),
    datas=collect_data_files("imageio_ffmpeg"),
    hiddenimports=["imageio_ffmpeg", "pymupdf"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Slide Recorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
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
    upx=True,
    upx_exclude=[],
    name="Slide Recorder",
)

app = BUNDLE(
    coll,
    name="Slide Recorder.app",
    icon=None,
    bundle_identifier="com.local.slide-recorder",
    info_plist={
        "NSMicrophoneUsageDescription": "Slide Recorder needs microphone access to record slide voiceovers.",
    },
)

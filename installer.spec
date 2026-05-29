# -*- mode: python ; coding: utf-8 -*-
# created using: venv/bin/pyi-makespec
import os
import re
from PyInstaller.utils.hooks import collect_all

# Read __version__ from src/__version__.py without importing the package
_version_ns = {}
with open(os.path.join('src', '__version__.py'), 'r', encoding='utf-8') as _f:
    exec(_f.read(), _version_ns)
__version__ = _version_ns['__version__']

# Validate semver and extract the numeric prefix for Windows file-version fields
_m = re.match(r'^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$', __version__)
if not _m:
    raise SystemExit(
        f"src/__version__.py: __version__ {__version__!r} is not valid semver "
        f"(expected MAJOR.MINOR.PATCH[-PRERELEASE])"
    )
_ver_tuple = tuple(int(x) for x in _m.groups()) + (0,)

# Windows version-info resource. EXE(version=...) ignores this on non-Windows builds,
# so the same spec is used on both platforms unchanged.
_version_info_path = os.path.join('build', 'version_info.txt')
os.makedirs(os.path.dirname(_version_info_path), exist_ok=True)
with open(_version_info_path, 'w', encoding='utf-8') as _f:
    _f.write(f"""\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_ver_tuple},
    prodvers={_ver_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('FileDescription', 'MTConnectExplorer'),
          StringStruct('FileVersion', '{__version__}'),
          StringStruct('InternalName', 'MTConnectExplorer'),
          StringStruct('OriginalFilename', 'MTConnectExplorer.exe'),
          StringStruct('ProductName', 'MTConnectExplorer'),
          StringStruct('ProductVersion', '{__version__}'),
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

datas = [
    ("src", "src") # Include src/ directory
]
binaries = []
hiddenimports = []
for pkg in [ # Imports, not packages
        'pydantic',
        'psutil',
        'streamlit',
        'streamlit_tree_select'
    ]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['src/__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=['hooks'],
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
    a.binaries,
    a.datas,
    [],
    name='MTConnectExplorer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_info_path,
)

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from pathlib import Path
root = Path(SPECPATH).parents[1]
a = Analysis([str(root / 'packaging/macos/desktop_engine.py')],
    pathex=[str(root / 'src')],
    datas=collect_data_files('image23mf'),
    hiddenimports=collect_submodules('image23mf') + ['uvicorn.logging', 'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on'],
    excludes=['pytest', 'hypothesis', 'tkinter', 'IPython', 'matplotlib', 'Cython'],
    noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='image23mf-engine',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, target_arch='arm64')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='image23mf-engine')

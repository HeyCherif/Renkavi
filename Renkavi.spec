# Build with scripts/build_release.py so third-party notices are collected first.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
datas = []
for directory, extensions in (
    ('frontend', {'.html', '.css', '.js', '.svg', '.png', '.ico', '.woff2', '.json'}),
    ('assets/fonts', {'.ttf', '.otf', '.txt'}),
    ('assets/brand', {'.svg', '.png', '.ico'}),
    ('backend', {'.py'}),
):
    for source in sorted((root / directory).rglob('*')):
        if source.is_file() and (source.suffix.lower() in extensions or source.name.endswith('LICENSE') or source.name.startswith('LICENSE')) and not {'__pycache__', 'custom'} & set(source.relative_to(root).parts):
            datas.append((str(source), str(source.relative_to(root).parent)))
license_dir = Path(os.environ['RENKAVI_LICENSE_DIR'])
datas.append((str(license_dir), 'licenses'))
for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
    if (root / name).is_file():
        datas.append((str(root / name), '.'))

binaries = []
hiddenimports = ['webview.platforms.edgechromium', 'webview.platforms.winforms',
                 'rapidocr', 'onnxruntime', 'ctranslate2', 'sentencepiece',
                 'pypdfium2', 'pypdfium2_raw', 'lxml.etree']
# These providers are loaded dynamically; their native DLLs and model/config data
# must be included even when no local OCR/translation job ran during analysis.
for package in ('rapidocr', 'onnxruntime', 'ctranslate2', 'sentencepiece', 'pypdfium2', 'pypdfium2_raw'):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

a = Analysis([str(root / 'run.py')], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['torch', 'torchvision', 'torchaudio', 'tensorflow',
                       'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter',
                       'webview.platforms.qt', 'webview.platforms.gtk',
                       'matplotlib', 'IPython', 'notebook', 'pytest'],
             noarchive=False)
# RapidOCR wheels can ship pretrained ONNX weights and hooks may collect them.
# Model installation is handled by the app's manager, never by the desktop ZIP.
weight_extensions = {'.onnx', '.pt', '.pth', '.ckpt', '.gguf', '.safetensors'}
a.datas = [entry for entry in a.datas if Path(entry[0]).suffix.lower() not in weight_extensions]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Renkavi',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          icon=str(root / 'assets' / 'brand' / 'renkavi.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Renkavi')

"""Build a single-file Kaggle bundle (SPEC 3.13).

Zips the ``v40_sorry_resolver`` package, base64-embeds it into
``dist/v40_kaggle_bundle.py``. On Kaggle the bundle unpacks itself to
``/kaggle/working/v40_src`` (fallback ``./v40_src``) and runs ``main()``.

Usage: ``python tools/make_kaggle_bundle.py`` — also self-checks the
generated artifact with ``py_compile``.
"""

from __future__ import annotations

import base64
import io
import os
import py_compile
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(REPO_ROOT, "v40_sorry_resolver")
DIST_DIR = os.path.join(REPO_ROOT, "dist")
OUT_FILE = os.path.join(DIST_DIR, "v40_kaggle_bundle.py")

HEADER = '''"""
v40 sorry resolver — single-file Kaggle bundle.

Self-contained: the ``v40_sorry_resolver`` package is embedded as a
base64 zip. On import of ``main()`` it unpacks to
``/kaggle/working/v40_src`` (fallback ``./v40_src`` when /kaggle is absent)
and dispatches to the package CLI.

Kaggle 12h budget mapping (SPEC 3.13 / README):
    wall_clock_budget_s = 36000 (10h, keep 2h headroom), num_workers = 16.
Run in a Kaggle notebook cell:
    %run /path/to/v40_kaggle_bundle.py --workers 16 --wall-clock-budget 36000
or:
    import v40_kaggle_bundle; v40_kaggle_bundle.main(["--workers", "16"])
"""

import base64
import io
import os
import sys
import zipfile

_BUNDLE_B64 = (
'''

FOOTER = '''
)


def unpack_bundle(dest_root=None):
    """Decode + extract the embedded package; return the source root."""
    if dest_root is None:
        dest_root = (
            "/kaggle/working/v40_src" if os.path.isdir("/kaggle") else "./v40_src"
        )
    os.makedirs(dest_root, exist_ok=True)
    blob = base64.b64decode("".join(_BUNDLE_B64))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(dest_root)
    if dest_root not in sys.path:
        sys.path.insert(0, dest_root)
    return dest_root


def main(argv=None):
    src = unpack_bundle()
    from v40_sorry_resolver.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _iter_package_files():
    for root, dirs, files in os.walk(PACKAGE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, REPO_ROOT)
            yield full, rel


def build_bundle() -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, rel in _iter_package_files():
            zf.write(full, rel)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    chunks = [b64[i : i + 76] for i in range(0, len(b64), 76)]
    body = HEADER + "".join(f'    "{c}"\n' for c in chunks) + FOOTER

    os.makedirs(DIST_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(body)
    return OUT_FILE


def main() -> int:
    out = build_bundle()
    py_compile.compile(out, doraise=True)
    size = os.path.getsize(out)
    print(f"bundle written: {out} ({size} bytes); py_compile OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

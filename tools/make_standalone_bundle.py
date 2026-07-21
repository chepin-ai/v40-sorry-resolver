"""Build the self-contained, self-installing single-file v40 bundle.

Generates ``dist/v40_standalone.py`` which embeds:

- the ``v40_sorry_resolver`` package (base64 zip), and
- the ``lean_mini_project`` self-test fixture (base64 zip, from
  ``/mnt/agents/output/lean_mini_project`` or ``--mini-project``;
  ``.git`` / ``.lake`` / ``__pycache__`` are excluded).

The generated file bootstraps itself *before* dispatching to the package
CLI (all skippable with ``--skip-bootstrap``):

1. Environment: ensures ``lake`` (installs elan + Lean 4.20.0; falls back
   to the ghfast.top proxy and to a pip-``zstandard``+tarfile manual
   toolchain unpack when GitHub/elan are unreachable) and pip deps
   (``openai``/``httpx`` always; ``lean-dojo``+``GitPython`` only for
   ``--verifier dojo/repl/hybrid/lean_interact``) via the tuna mirror with
   PyPI fallback. Idempotent: a warm machine skips in seconds.
2. Keys: environment variables > ``kaggle_secrets`` > sibling ``.env``.
   No keys at all -> WARNING and only ``--mock-llm`` / ``--self-test``
   (or ``--help``) are allowed.
3. Task source: ``--project github:owner/repo[/subdir][@ref]`` is cloned
   (``git clone --depth 1``, ``GITHUB_TOKEN`` honoured, codeload zip via
   ghfast fallback) into the work dir and scanned.
4. ``--self-test``: unpacks the embedded mini project, runs the full
   pipeline (default ``--mock-llm`` + real subprocess verifier, zero API
   cost; ``--real-llm`` uses real keys), prints the baseline comparison
   (expect >=7/11 solved, verify_pass_rate=1.0, 2 Hard sorries rejected)
   and exits 0/1 accordingly.

Usage: ``python tools/make_standalone_bundle.py [--mini-project PATH]
[--out PATH]`` — also self-checks the artifact with ``py_compile``.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import py_compile
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(REPO_ROOT, "v40_sorry_resolver")
DIST_DIR = os.path.join(REPO_ROOT, "dist")
OUT_FILE = os.path.join(DIST_DIR, "v40_standalone.py")
DEFAULT_MINI_PROJECT = "/mnt/agents/output/lean_mini_project"

# Everything above ``_PKG_B64`` in the generated file.
HEADER = '''"""
v40 sorry resolver - self-contained standalone bundle.

Single file = bootstrap logic + base64(zip(v40_sorry_resolver))
+ base64(zip(lean_mini_project)). It installs its own toolchain
(elan + Lean 4.20.0), pip deps and API keys, then runs the full CLI.

Quick start:
    python v40_standalone.py --self-test            # zero-cost end-to-end check
    python v40_standalone.py --project /path/to/lean/project
    python v40_standalone.py --project github:owner/repo[/subdir][@ref]
Kaggle:
    %run v40_standalone.py --project github:owner/repo --workers 16 \\
        --wall-clock-budget 36000
Useful flags (consumed by this bootstrap, not the package CLI):
    --skip-bootstrap   do not touch the environment / keys / network
    --self-test        run the embedded mini project through the real pipeline
    --real-llm         (with --self-test) use real provider keys, not the mock
Everything else is forwarded to ``v40_sorry_resolver.cli`` (see --help).
"""

import base64
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

_PKG_B64 = (
'''

# Between the two blobs.
MIDDLE = ''')

_MINI_PROJECT_B64 = (
'''

# All bootstrap logic + main().
FOOTER = ''')

LEAN_VERSION = "4.20.0"
LEAN_TOOLCHAIN = "leanprover/lean4:v4.20.0"
GHFAST_PREFIX = "https://ghfast.top/"
TUNA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PYPI_INDEX = "https://pypi.org/simple"
ELAN_INIT_URLS = (
    "https://elan.lean-lang.org/elan-init.sh",
    "https://release.lean-lang.org/elan/elan-init.sh",
)
# Secrets the bootstrap tries to hydrate (env > kaggle_secrets > .env).
SECRET_NAMES = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY_2",
    "KIMI_API_KEY",
    "LONGCAT_API_KEY",
    "GITHUB_TOKEN",
)
LLM_KEY_NAMES = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY_2",
    "KIMI_API_KEY",
    "LONGCAT_API_KEY",
)
# Verifier backends whose optional pip deps are installed on demand.
OPTIONAL_DEP_VERIFIERS = ("dojo", "repl", "hybrid", "lean_interact")
# Flags consumed by this bootstrap and stripped before the package CLI.
BOOTSTRAP_FLAGS_WITH_VALUE = ()
BOOTSTRAP_FLAGS_BOOL = ("--skip-bootstrap", "--self-test", "--real-llm")

_GITHUB_SPEC_RE = re.compile(
    r"^github:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?P<subdir>(?:/[A-Za-z0-9_./-]+?)?)(?:@(?P<ref>[A-Za-z0-9_./-]+))?$"
)


def _log(msg):
    print(f"[v40-bootstrap] {msg}", flush=True)


def _warn(msg):
    print(f"[v40-bootstrap] WARNING: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------- flag parsing


def strip_bootstrap_flags(argv):
    """Remove bootstrap-only flags; return (cleaned_argv, flags:set)."""
    flags = set()
    cleaned = []
    for tok in argv:
        if tok in BOOTSTRAP_FLAGS_BOOL:
            flags.add(tok.lstrip("-").replace("-", "_"))
        else:
            cleaned.append(tok)
    return cleaned, flags


def _argv_has_flag(argv, *names):
    return any(tok in names or any(tok.startswith(n + "=") for n in names) for tok in argv)


def _argv_value(argv, *names):
    """Value of the last occurrence of --name VALUE or --name=VALUE."""
    value = None
    it = iter(range(len(argv)))
    for i in it:
        tok = argv[i]
        for name in names:
            if tok == name and i + 1 < len(argv):
                value = argv[i + 1]
            elif tok.startswith(name + "="):
                value = tok.split("=", 1)[1]
    return value


def needs_optional_verifier_deps(argv):
    """True when --verifier selects a backend with optional pip deps."""
    val = (_argv_value(argv, "--verifier") or "").strip().lower()
    return val in OPTIONAL_DEP_VERIFIERS


# ----------------------------------------------------------------- pip helper


def _pip_install(packages, index_url=TUNA_INDEX, timeout=600):
    """pip install via tuna mirror, falling back to official PyPI."""
    for index in (index_url, PYPI_INDEX, None):
        cmd = [sys.executable, "-m", "pip", "install", "-q"]
        if index:
            cmd += ["-i", index]
        cmd += list(packages)
        try:
            proc = subprocess.run(cmd, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - network oddities
            _warn(f"pip install via {index or 'default index'} failed: {exc}")
            continue
        if proc.returncode == 0:
            return True
        _warn(f"pip install via {index or 'default index'} exited {proc.returncode}")
    return False


def _ensure_module(module, packages):
    try:
        __import__(module)
        return True
    except ImportError:
        pass
    _log(f"installing missing python deps: {', '.join(packages)}")
    if not _pip_install(packages):
        return False
    try:
        __import__(module)
        return True
    except ImportError:
        return False


# -------------------------------------------------------------- download util


class _StalledDownload(Exception):
    """Raised when a mirror connects but transfers too slowly."""


def _download(urls, dest, timeout=300, min_rate_kbps=32, stall_window=20):
    """Try each URL in order (direct first, ghfast proxy fallback).

    A mirror that connects but sustains < ``min_rate_kbps`` KiB/s after a
    ``stall_window`` seconds grace period counts as failed, so a crawling
    direct link quickly falls through to the proxy mirror.
    """
    import time

    last = None
    for url in urls:
        try:
            _log(f"downloading {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "v40-standalone"})
            start = time.monotonic()
            got = 0
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(
                dest, "wb"
            ) as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    elapsed = time.monotonic() - start
                    if elapsed > stall_window:
                        rate = got / 1024.0 / elapsed
                        if rate < min_rate_kbps:
                            raise _StalledDownload(
                                f"{rate:.1f} KiB/s < {min_rate_kbps} KiB/s"
                            )
            if got > 0 and os.path.getsize(dest) > 0:
                _log(f"downloaded {got / 1e6:.1f} MB in {time.monotonic() - start:.0f}s")
                return url
        except Exception as exc:  # noqa: BLE001
            last = exc
            _warn(f"download failed ({url}): {exc}")
    raise RuntimeError(f"all download mirrors failed; last error: {last}")


def _with_ghfast(url):
    return [url, GHFAST_PREFIX + url]


# ------------------------------------------------------------ lean toolchain


def _lake_on_path():
    return shutil.which("lake")


def _prepend_path(directory):
    path = os.environ.get("PATH", "")
    if directory and directory not in path.split(os.pathsep):
        os.environ["PATH"] = directory + os.pathsep + path


def _lean_asset_name(version=LEAN_VERSION):
    plat = sys.platform
    machine = (os.uname().machine if hasattr(os, "uname") else "x86_64").lower()
    arch = "aarch64" if machine in ("aarch64", "arm64") else ""
    if plat == "darwin":
        suffix = "macos_aarch64" if arch else "macos"
        ext = "tar.zst"
    elif plat.startswith("linux"):
        suffix = "linux_aarch64" if arch else "linux"
        ext = "tar.zst"
    elif plat.startswith("win"):
        suffix, ext = "windows", "zip"
    else:  # best guess
        suffix, ext = "linux", "tar.zst"
    return f"lean-{version}-{suffix}.{ext}"


def _elan_home():
    return os.environ.get("ELAN_HOME") or os.path.join(os.path.expanduser("~"), ".elan")


def _elan_toolchain_dir():
    return os.path.join(
        _elan_home(), "toolchains", LEAN_TOOLCHAIN.replace("/", "--").replace(":", "---")
    )


def _try_elan_install():
    """Install elan (+ default toolchain) via the official shell script."""
    tmpdir = tempfile.mkdtemp(prefix="v40-elan-")
    script = os.path.join(tmpdir, "elan-init.sh")
    try:
        _download(list(ELAN_INIT_URLS), script, timeout=120)
        try:
            proc = subprocess.run(
                ["sh", script, "-y", "--default-toolchain", LEAN_TOOLCHAIN],
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            _warn("elan installer stalled (>300s); treating as failed")
            return False
        _prepend_path(os.path.join(_elan_home(), "bin"))
        return proc.returncode == 0 and _lake_on_path() is not None
    except Exception as exc:  # noqa: BLE001
        _warn(f"elan route failed: {exc}")
        return False


def _ensure_zstandard():
    if shutil.which("zstd") or shutil.which("unzstd"):
        return "binary"
    try:
        import zstandard  # noqa: F401

        return "python"
    except ImportError:
        pass
    _log("no zstd binary; installing python zstandard for tarfile unpack")
    if _pip_install(["zstandard"]):
        try:
            import zstandard  # noqa: F401

            return "python"
        except ImportError:
            pass
    return None


def _unpack_tar_zst(archive, dest_dir):
    """Unpack a .tar.zst via the zstd binary or python-zstandard+tarfile."""
    mode = _ensure_zstandard()
    if mode is None:
        raise RuntimeError("no zstd available (binary or pip zstandard)")
    os.makedirs(dest_dir, exist_ok=True)
    if mode == "binary":
        zstd = shutil.which("zstd") or shutil.which("unzstd")
        out = subprocess.run(
            [zstd, "-d", "-c", archive], stdout=subprocess.PIPE, check=True
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as tf:
            _extractall(tf, dest_dir)
    else:
        import zstandard

        with open(archive, "rb") as fh:
            dctx = zstandard.ZstdDecompressor()
            with dctx.stream_reader(fh) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tf:
                    _extractall(tf, dest_dir)


def _extractall(tf, dest_dir):
    try:
        tf.extractall(dest_dir, filter="tar")
    except TypeError:  # Python < 3.12 has no filter argument
        tf.extractall(dest_dir)


def _install_lean_manually():
    """Download the Lean release tarball (ghfast fallback) and unpack it.

    If elan exists we drop the toolchain into ELAN_HOME so the elan shim
    resolves it; otherwise we put the bare toolchain bin/ on PATH.
    """
    asset = _lean_asset_name()
    base = (
        f"https://github.com/leanprover/lean4/releases/download/"
        f"v{LEAN_VERSION}/{asset}"
    )
    tmpdir = tempfile.mkdtemp(prefix="v40-lean-")
    archive = os.path.join(tmpdir, asset)
    _download(_with_ghfast(base), archive, timeout=1800)
    extract_root = os.path.join(tmpdir, "x")
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_root)
    else:
        _unpack_tar_zst(archive, extract_root)
    entries = [os.path.join(extract_root, d) for d in os.listdir(extract_root)]
    top = next((e for e in entries if os.path.isdir(e)), extract_root)
    if not os.path.isfile(os.path.join(top, "bin", "lake")):
        raise RuntimeError(f"unpacked Lean archive has no bin/lake under {top}")
    elan_bin = os.path.join(_elan_home(), "bin")
    if os.path.isdir(elan_bin):
        dest = _elan_toolchain_dir()
        if not os.path.isdir(dest):
            shutil.move(top, dest)
        _prepend_path(elan_bin)
    else:
        dest = os.path.join(os.path.expanduser("~"), ".v40", "toolchains", "lean")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.isdir(dest):
            shutil.move(top, dest)
        _prepend_path(os.path.join(dest, "bin"))
    return _lake_on_path() is not None


def ensure_lean():
    """Idempotent: no-op (fast) when lake is already on PATH."""
    if _lake_on_path():
        _log(f"lake found: {_lake_on_path()} (skip install)")
        return True
    _log(f"lake not found; installing Lean {LEAN_VERSION} toolchain...")
    if _try_elan_install():
        _log("elan + Lean installed")
        return True
    _warn("elan route unavailable; trying direct toolchain download (ghfast proxy)")
    try:
        if _install_lean_manually():
            _log(f"Lean {LEAN_VERSION} installed manually")
            return True
    except Exception as exc:  # noqa: BLE001
        _warn(f"manual Lean install failed: {exc}")
    return False


def ensure_python_deps(argv):
    ok = True
    if not _ensure_module("openai", ["openai>=2.46,<3", "httpx>=0.28,<1"]):
        _warn("openai/httpx unavailable; real LLM providers will not work")
        ok = False
    if not _ensure_module("httpx", ["httpx>=0.28,<1"]):
        ok = False
    if needs_optional_verifier_deps(argv):
        if not _ensure_module("lean_dojo", ["lean-dojo>=4.20,<5", "GitPython>=3.1,<4"]):
            _warn("lean-dojo/GitPython unavailable for the selected verifier")
            ok = False
        _ensure_module("git", ["GitPython>=3.1,<4"])
    return ok


# -------------------------------------------------------------- key bootstrap


def _parse_env_file(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _kaggle_secret(name):
    try:
        from kaggle_secrets import UserSecretsClient

        return UserSecretsClient().get_secret(name)
    except Exception:  # noqa: BLE001 - not on Kaggle / secret absent
        return None


def bootstrap_keys(argv, env_file=None):
    """Priority: process env > kaggle_secrets > sibling .env.

    Returns True when at least one LLM key is available afterwards.
    """
    if env_file is None:
        env_file = os.path.join(
            os.path.dirname(os.path.abspath(sys.argv[0] or ".")), ".env"
        )
    file_vars = _parse_env_file(env_file)
    for name in SECRET_NAMES:
        if os.environ.get(name):
            continue
        secret = _kaggle_secret(name)
        if secret:
            os.environ[name] = secret
            continue
        if file_vars.get(name):
            os.environ[name] = file_vars[name]
    if any(os.environ.get(k) for k in LLM_KEY_NAMES):
        return True
    _warn(
        "no LLM API keys found (env / kaggle_secrets / .env); "
        "only --mock-llm and --self-test are allowed"
    )
    return False


# ---------------------------------------------------------- github: task source


def parse_github_spec(spec):
    """Parse github:owner/repo[/subdir][@ref] -> dict or None."""
    m = _GITHUB_SPEC_RE.match(spec.strip())
    if not m:
        return None
    subdir = (m.group("subdir") or "").strip("/")
    return {
        "owner": m.group("owner"),
        "repo": m.group("repo"),
        "subdir": subdir,
        "ref": m.group("ref"),
    }


def _clone_urls(owner, repo):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    auth = f"x-access-token:{token}@" if token else ""
    plain = f"https://github.com/{owner}/{repo}.git"
    urls = [f"https://{auth}github.com/{owner}/{repo}.git"]
    # Proxy fallback must not embed credentials in the logged URL path.
    proxied = GHFAST_PREFIX + plain
    if token:
        proxied = GHFAST_PREFIX + f"https://{auth}github.com/{owner}/{repo}.git"
    urls.append(proxied)
    return urls


def _codeload_urls(owner, repo, ref):
    base = f"https://codeload.github.com/{owner}/{repo}/zip/{ref or 'HEAD'}"
    return [base, GHFAST_PREFIX + base]


def fetch_github_project(spec, dest_root):
    """Clone (shallow) or zip-download a github: source; return local path
    including the optional subdir. Raises on total failure."""
    parsed = parse_github_spec(spec)
    if parsed is None:
        raise ValueError(f"invalid github task source: {spec!r}")
    owner, repo, ref = parsed["owner"], parsed["repo"], parsed["ref"]
    dest = os.path.join(dest_root, f"gh_{owner}_{repo}")
    if not os.path.isdir(dest):
        cloned = False
        for url in _clone_urls(owner, repo):
            cmd = ["git", "clone", "--depth", "1"]
            if ref:
                cmd += ["--branch", ref]
            cmd += [url, dest]
            display = url.replace(os.environ.get("GITHUB_TOKEN", "!!!"), "***")
            _log(f"git clone {display}")
            proc = subprocess.run(cmd, timeout=900)
            if proc.returncode == 0:
                cloned = True
                break
            _warn(f"git clone failed (rc={proc.returncode})")
            shutil.rmtree(dest, ignore_errors=True)
        if not cloned:
            _log("falling back to codeload zip download")
            archive = os.path.join(dest_root, f"gh_{owner}_{repo}.zip")
            _download(_codeload_urls(owner, repo, ref), archive, timeout=900)
            staging = dest + "_x"
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(staging)
            tops = [os.path.join(staging, d) for d in os.listdir(staging)]
            top = next((t for t in tops if os.path.isdir(t)), staging)
            shutil.move(top, dest)
            shutil.rmtree(staging, ignore_errors=True)
    else:
        _log(f"reusing existing clone {dest}")
    local = os.path.join(dest, parsed["subdir"]) if parsed["subdir"] else dest
    if not os.path.isdir(local):
        raise FileNotFoundError(f"github subdir not found after clone: {local}")
    return local


def resolve_github_sources(argv, work_dir):
    """Rewrite github: --project/--project-paths values to local clones."""
    out = list(argv)
    gh_root = os.path.join(work_dir, "github_sources")
    for i, tok in enumerate(out):
        value = None
        if tok in ("--project", "--project-paths") and i + 1 < len(out):
            value, idx = out[i + 1], i + 1
        elif tok.startswith("--project=") or tok.startswith("--project-paths="):
            value, idx = tok.split("=", 1)[1], i
        if value and value.startswith("github:"):
            os.makedirs(gh_root, exist_ok=True)
            local = fetch_github_project(value, gh_root)
            if "=" in out[idx] and out[idx].startswith("--"):
                out[idx] = out[idx].split("=", 1)[0] + "=" + local
            else:
                out[idx] = local
    return out


# ------------------------------------------------------------------- unpack


def _default_work_root():
    return "/kaggle/working" if os.path.isdir("/kaggle") else os.path.abspath(".")


def unpack_bundle(dest_root=None):
    """Decode + extract the embedded package; return the source root."""
    if dest_root is None:
        dest_root = os.path.join(_default_work_root(), "v40_src")
    os.makedirs(dest_root, exist_ok=True)
    blob = base64.b64decode("".join(_PKG_B64))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(dest_root)
    if dest_root not in sys.path:
        sys.path.insert(0, dest_root)
    return dest_root


def unpack_mini_project(dest_root):
    """Extract the embedded lean_mini_project; return its root path."""
    dest = os.path.join(dest_root, "lean_mini_project")
    os.makedirs(dest, exist_ok=True)
    blob = base64.b64decode("".join(_MINI_PROJECT_B64))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(dest)
    return dest


# ------------------------------------------------------------------ self-test

SELF_TEST_MIN_SOLVED = 7
SELF_TEST_TOTAL = 11
SELF_TEST_HARD_REJECTS = 2


def _scan_hard_task_ids(project):
    """Scan the mini project and return task ids living in Hard.lean.

    Returns None when scanning is unavailable (caller falls back to a
    heuristic on task ids)."""
    try:
        import asyncio

        from v40_sorry_resolver.sorrydb import SorryScanner

        try:
            scanner = SorryScanner([project])
        except TypeError:
            scanner = SorryScanner()

        async def _go():
            res = scanner.scan([project])
            if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                res = await res
            return res

        tasks = asyncio.run(_go()) or []
        return {
            str(getattr(t, "id", ""))
            for t in tasks
            if "Hard" in str(getattr(t, "file_path", ""))
        }
    except Exception:  # noqa: BLE001
        return None


def evaluate_self_test(report, hard_ids=None):
    """Check a run-report dict against the printed baseline.

    Returns (ok, reasons:list[str])."""
    reasons = []
    solved = int(report.get("solved", 0))
    vpr = float(report.get("verify_pass_rate", 0.0))
    results = report.get("results", []) or []
    if hard_ids is not None:
        hard = [r for r in results if str(r.get("task_id", "")) in hard_ids]
    else:  # heuristic fallback: task ids that embed the file name
        hard = [r for r in results if "Hard" in str(r.get("task_id", ""))]
    hard_rejected = sum(1 for r in hard if not r.get("success"))
    if solved < SELF_TEST_MIN_SOLVED:
        reasons.append(f"solved {solved} < {SELF_TEST_MIN_SOLVED}")
    if abs(vpr - 1.0) > 1e-9:
        reasons.append(f"verify_pass_rate {vpr} != 1.0")
    if hard_rejected != SELF_TEST_HARD_REJECTS:
        reasons.append(
            f"Hard rejects {hard_rejected} != {SELF_TEST_HARD_REJECTS}"
        )
    return (not reasons, reasons)


def run_self_test(argv, work_root):
    """Full pipeline on the embedded mini project; exit code reflects pass."""
    import glob
    import json

    tmp = tempfile.mkdtemp(prefix="v40-selftest-", dir=None)
    project = unpack_mini_project(tmp)
    work_dir = os.path.join(work_root, "v40_selftest_work")
    os.makedirs(work_dir, exist_ok=True)
    real_llm = "--real-llm" in argv
    cli_args = [
        "--project",
        project,
        "--verifier",
        "subprocess",
        "--output-dir",
        work_dir,
        "--no-resume",
    ]
    if not real_llm:
        cli_args.append("--mock-llm")
    _log(
        "self-test: running full pipeline on embedded lean_mini_project "
        f"({'real LLM keys' if real_llm else 'mock LLM, zero API cost'})"
    )
    src = unpack_bundle(os.path.join(work_root, "v40_src"))
    from v40_sorry_resolver.cli import main as cli_main

    rc = cli_main(cli_args)
    reports = sorted(
        glob.glob(os.path.join(work_dir, "results", "run_*.json")), reverse=True
    )
    report = {}
    for path in reports:
        if path.endswith("_summary.txt"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                report = json.load(fh)
            break
        except Exception:  # noqa: BLE001
            continue
    hard_ids = _scan_hard_task_ids(project)
    ok, reasons = evaluate_self_test(report, hard_ids=hard_ids)
    solved = int(report.get("solved", 0))
    vpr = float(report.get("verify_pass_rate", 0.0))
    results = report.get("results") or []
    if hard_ids is not None:
        hard_rej = sum(
            1 for r in results if str(r.get("task_id", "")) in hard_ids and not r.get("success")
        )
    else:
        hard_rej = sum(
            1 for r in results if "Hard" in str(r.get("task_id", "")) and not r.get("success")
        )
    print("=== v40 self-test baseline comparison ===")
    print(f"solved: {solved}/{SELF_TEST_TOTAL} (baseline >= {SELF_TEST_MIN_SOLVED})")
    print(f"verify_pass_rate: {vpr:.2f} (baseline 1.00)")
    print(
        f"Hard rejections: {hard_rej}"
        f" (baseline {SELF_TEST_HARD_REJECTS}; unprovable sorries must be rejected)"
    )
    if rc != 0:
        ok = False
        reasons.append(f"pipeline exited rc={rc}")
    if ok:
        print("SELF-TEST PASS")
        return 0
    print("SELF-TEST FAIL: " + "; ".join(reasons or ["no report produced"]))
    return 1


# ---------------------------------------------------------------------- main


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if _argv_has_flag(argv, "--help", "-h"):
        src = unpack_bundle()
        from v40_sorry_resolver.cli import main as cli_main

        return cli_main(["--help"])

    work_root = _default_work_root()
    if not _argv_has_flag(argv, "--skip-bootstrap"):
        if not ensure_lean():
            _warn(
                "Lean toolchain unavailable; real verification will fail. "
                "Install elan manually or re-run with network access."
            )
        ensure_python_deps(argv)
        have_keys = bootstrap_keys(argv)
    else:
        _log("--skip-bootstrap: environment/keys untouched")
        have_keys = any(os.environ.get(k) for k in LLM_KEY_NAMES)

    if _argv_has_flag(argv, "--self-test"):
        return run_self_test(argv, work_root)

    if not have_keys and not _argv_has_flag(argv, "--mock-llm", "--dry-run"):
        _warn("refusing to run without API keys (use --mock-llm for a dry exercise)")
        return 2

    try:
        argv = resolve_github_sources(argv, os.path.join(work_root, "v40_work"))
    except Exception as exc:  # noqa: BLE001
        _warn(f"github task source failed: {exc}")
        return 2

    cleaned, _ = strip_bootstrap_flags(argv)
    src = unpack_bundle()
    try:
        from v40_sorry_resolver.cli import main as cli_main
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"v40 standalone: missing dependency {exc.name!r}; re-run without "
            "--skip-bootstrap so the bootstrap can install it"
        )
    return cli_main(cleaned)


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


def _iter_mini_project_files(mini_root):
    for root, dirs, files in os.walk(mini_root):
        dirs[:] = [
            d for d in dirs if d not in (".git", ".lake", "__pycache__")
        ]
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, mini_root)
            yield full, rel


def _zip_b64(entries, arcname_root=None):
    """entries: iterable of (abs_path, rel_arcname). Return base64 str."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, rel in entries:
            arc = os.path.join(arcname_root, rel) if arcname_root else rel
            zf.write(full, arc)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _format_b64(b64):
    chunks = [b64[i : i + 76] for i in range(0, len(b64), 76)]
    return "".join(f'    "{c}"\n' for c in chunks)


def build_bundle(mini_project=DEFAULT_MINI_PROJECT, out_file=OUT_FILE):
    if not os.path.isdir(mini_project):
        raise FileNotFoundError(f"mini project not found: {mini_project}")
    pkg_b64 = _zip_b64(_iter_package_files())
    mini_b64 = _zip_b64(_iter_mini_project_files(mini_project))
    body = HEADER + _format_b64(pkg_b64) + MIDDLE + _format_b64(mini_b64) + FOOTER
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as fh:
        fh.write(body)
    return out_file


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mini-project", default=DEFAULT_MINI_PROJECT)
    parser.add_argument("--out", default=OUT_FILE)
    args = parser.parse_args(argv)
    out = build_bundle(args.mini_project, args.out)
    py_compile.compile(out, doraise=True)
    size = os.path.getsize(out)
    print(f"standalone bundle written: {out} ({size} bytes); py_compile OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

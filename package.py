"""Build Classark Exam Builder for shipping: a Windows program, a macOS app, or a zip.

    uv run --project engine python package.py exe   # dist/ClassarkExamBuilder/ClassarkExamBuilder.exe
    uv run --project engine python package.py mac   # dist/Classark Exam Builder-<version>.dmg (on a Mac)
    uv run --project engine python package.py zip   # dist/ClassarkExamBuilder-<version>.zip
    uv run --project engine python package.py installer
        # the exe build, plus dist/ClassarkExamBuilder-Setup-<version>.exe (Inno Setup 6)
        # and dist/ClassarkExamBuilder-<version>.msi (WiX Toolset 5)

Every target builds the debug edition unless --release is given (qbx/edition.py, user-asked):

    debug     Claude Code on this computer and the Settings screen - for testing
    release   neither - for teachers (they'll reach Claude through a Classark account)

The mac build signs with your Developer ID and notarises when these are set:

    QBX_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
    QBX_NOTARY_PROFILE=qbx      # once: xcrun notarytool store-credentials qbx --apple-id ... --team-id ...

Without them it builds an unsigned app, which only opens on the Mac that built it.

On Windows the exe, both installers and the .exe installer's uninstaller are code signed
when one of these is set (checked in this order), and left unsigned otherwise:

    QBX_SIGN_COMMAND="..."          # any signing command, {file} where the file goes - for a
                                    # cloud or hardware-token certificate (Azure Trusted Signing...)
    QBX_SIGN_THUMBPRINT=ab12...     # a certificate installed in Windows' certificate store
    QBX_SIGN_PFX=C:/path/cert.pfx   # a certificate file, with QBX_SIGN_PFX_PASSWORD
    QBX_SIGN_TIMESTAMP=<url>        # optional; default http://timestamp.digicert.com
    QBX_SIGNTOOL=<path>             # optional; default the newest Windows SDK signtool.exe

The exe needs nothing installed at all: Python, the libraries, the backend and the built UI
are all inside it, and double-clicking it starts the app (packaging/launcher.py). The zip is
the lighter alternative - the recipient needs `uv`, and start.bat sets up the rest.

What goes in is listed explicitly rather than copying the tree and pruning it, so nothing
private can slip in: no papers/, out/ or quizzes/ (CLAUDE.md: the tool ships empty, and
papers never leave the machine), no virtualenv, no node_modules, no IDE files.

Every build carries its licence (AGPL-3.0, because PyMuPDF is), every bundled library's
licence, and this version's complete source as a zip (qbx/legal.py, build/legal/) - the
source is what git tracks, so build from a committed tree.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
ENGINE = ROOT / "engine"
UI = ROOT / "ui"
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
# the app's logo, which the UI shows too - the exe's icon is rendered from it at build time
ICON_SVG = UI / "public" / "icon.svg"

# the name people see once it's installed: the Mac app, the Windows installers' shortcuts
# and Settings > Apps entry, and the description on code signatures
APP_NAME = "Classark Exam Builder"
# the same, as a file name: the Windows exe and its folder, the zip
EXE_NAME = "ClassarkExamBuilder"
MAC_BUNDLE_ID = "com.classark.exambuilder"

# the engine's files the app needs to run - its tests and scratch scripts stay behind
ENGINE_FILES = ["main.py", "pyproject.toml", "uv.lock"]
ENGINE_PACKAGE = "qbx"
# "debug" or "release", set from the command line (main); qbx/edition.py reads it back from
# edition.txt beside itself in the build
EDITION = "debug"


def edition_file() -> Path:
    """build/edition/edition.txt, holding this build's edition, bundled as qbx/edition.txt."""
    path = ROOT / "build" / "edition" / "edition.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EDITION, encoding="utf-8")
    return path


def legal_folder() -> Path:
    """build/legal: the version, the AGPL text, the notices, every bundled library's
    licence and this version's complete source as a zip (qbx/legal.py), bundled as
    qbx/legal. The source is what git tracks, so build from a clean, committed tree."""
    sys.path.insert(0, str(ENGINE))
    from qbx import legal

    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout
    if dirty.strip():
        print("warning: uncommitted changes - the bundled source is what git tracks, which may not match this build")
    out = legal.write_legal_folder(ROOT / "build" / "legal", ROOT)
    print(f"legal: source.zip {(out / 'source.zip').stat().st_size // 1024} KB, licences and notices")
    return out


def build_ui() -> Path:
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    print("building the UI...")
    subprocess.run([npm, "run", "build"], cwd=UI, check=True)
    dist = UI / "dist"
    if not (dist / "index.html").exists():
        sys.exit("UI build produced no dist/index.html")
    return dist


def build_exe(ui_dist: Path, out_dir: Path = DIST) -> None:
    """ClassarkExamBuilder/ClassarkExamBuilder.exe via PyInstaller, windowed (no console):
    the launcher shows the app in its own window instead.

    A folder (--onedir), not one file. A --onefile exe unpacks its whole bundle - Python,
    PyMuPDF, the UI, ~150 MB - into %TEMP% on every launch, and Defender scans each file
    it writes: the user found startup slow, though the server itself is ready ~0.1s after
    Python starts (app.log). From a folder nothing is unpacked; ship it zipped."""
    import PyInstaller.__main__

    exe = out_dir / EXE_NAME / f"{EXE_NAME}.exe"
    if exe.exists():
        try:
            with open(exe, "ab"):
                pass
        except PermissionError:
            sys.exit(
                f"{exe} is in use - close Classark Exam Builder and "
                "build again, or build elsewhere with --out <folder>."
            )

    PyInstaller.__main__.run([*_pyinstaller_args(ui_dist, out_dir), "--name", EXE_NAME])
    size = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file())
    print(f"wrote {exe} (folder {size // (1024 * 1024)} MB) - give people the whole folder, zipped")
    for old in (out_dir / "QuestionBankBuilder.exe", out_dir / "QuestionBankBuilder"):
        if old.exists():
            print(f"note: {old} is a build from before the app was renamed - {exe.parent} replaces it")


def _signtool() -> str:
    """signtool.exe: QBX_SIGNTOOL, else the newest in the Windows SDK, else from PATH."""
    if os.environ.get("QBX_SIGNTOOL"):
        return os.environ["QBX_SIGNTOOL"]
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    found = sorted(kits.glob("10.*/x64/signtool.exe"), key=lambda p: [int(n) for n in p.parent.parent.name.split(".")])
    return str(found[-1]) if found else "signtool"


def sign_command(file: str) -> list[str] | None:
    """The command that signs `file`, from the QBX_SIGN_* settings; None if signing isn't
    set up. Every signature is SHA-256 and timestamped, so it stays valid after the
    certificate expires."""
    custom = os.environ.get("QBX_SIGN_COMMAND")
    if custom:
        return [part.replace("{file}", file) for part in custom.split()]
    timestamp = os.environ.get("QBX_SIGN_TIMESTAMP", "http://timestamp.digicert.com")
    base = [_signtool(), "sign", "/fd", "sha256", "/tr", timestamp, "/td", "sha256", "/d", APP_NAME]
    if os.environ.get("QBX_SIGN_THUMBPRINT"):
        return [*base, "/sha1", os.environ["QBX_SIGN_THUMBPRINT"], file]
    if os.environ.get("QBX_SIGN_PFX"):
        password = os.environ.get("QBX_SIGN_PFX_PASSWORD")
        return [*base, "/f", os.environ["QBX_SIGN_PFX"], *(["/p", password] if password else []), file]
    return None


def sign(file: Path) -> bool:
    cmd = sign_command(str(file))
    if cmd is None:
        return False
    print(f"signing {file.name}")
    subprocess.run(cmd, check=True)
    return True


def build_installers(version: str, out_dir: Path = DIST) -> None:
    """Both installers from the exe build in out_dir/ClassarkExamBuilder: an .exe made with
    Inno Setup (packaging/installer.iss) and an MSI made with WiX (packaging/installer.wxs).
    Each installs for the current user, with no admin prompt, and registers with Windows
    for Settings > Apps and Control Panel. The exe inside is signed before either is built,
    so both carry the signed copy."""
    app = out_dir / EXE_NAME
    icon = ROOT / "build" / "icon.ico"
    marker = PACKAGING / "installed.txt"
    signed = sign(app / f"{EXE_NAME}.exe")

    iscc = shutil.which("iscc") or next(
        (str(p) for p in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
            Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
        ) if p.exists()),
        None,
    )
    if not iscc:
        sys.exit("Inno Setup 6 isn't installed: winget install --id JRSoftware.InnoSetup -e")
    inno = [iscc, f"/DAppVersion={version}", f"/DSourceDir={app}", f"/DOutputDir={out_dir}", f"/DIconFile={icon}"]
    if signed:
        # Inno signs the setup and its uninstaller itself; $f is the file, $q a quote
        template = " ".join(f'"{part}"' if " " in part else part for part in sign_command("$f") or [])
        inno += ["/DSign", f"/Sqbxsign={template.replace(chr(34), '$q')}"]
    _run(*inno, str(PACKAGING / "installer.iss"))

    # a .NET global tool lives in ~/.dotnet/tools, which a terminal opened before installing
    # it doesn't have on PATH yet
    wix = shutil.which("wix") or next(
        (str(p) for p in [Path.home() / ".dotnet" / "tools" / "wix.exe"] if p.exists()), None
    )
    if not wix:
        sys.exit("WiX Toolset 5 isn't installed: dotnet tool install --global wix --version 5.0.2")
    msi = out_dir / f"ClassarkExamBuilder-{version}.msi"
    _run(wix, "build", str(PACKAGING / "installer.wxs"), "-arch", "x64",
         "-ext", "WixToolset.UI.wixext", "-ext", "WixToolset.Util.wixext",
         "-d", f"Version={version}", "-d", f"SourceDir={app}", "-d", f"IconFile={icon}", "-d", f"MarkerFile={marker}",
         "-o", str(msi))
    msi.with_suffix(".wixpdb").unlink(missing_ok=True)
    sign(msi)
    state = "signed" if signed else "unsigned - set QBX_SIGN_* to sign them"
    print(f"wrote {out_dir / f'ClassarkExamBuilder-Setup-{version}.exe'} and {msi} ({state})")


def windows_icon(out: Path) -> Path:
    """ICON_SVG as a Windows .ico, each size rendered from the vector (PyMuPDF opens SVG)
    rather than scaled down from one bitmap, so the small ones stay sharp. An .ico may hold
    PNGs as they are: a 6-byte header, a 16-byte entry per image, then the images."""
    import struct

    import pymupdf

    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    with pymupdf.open(ICON_SVG) as svg:
        page = svg[0]
        pngs = [
            page.get_pixmap(matrix=pymupdf.Matrix(size / page.rect.width, size / page.rect.height), alpha=True).tobytes(
                "png"
            )
            for size in sizes
        ]
    offset = 6 + 16 * len(sizes)
    entries = b""
    for size, png in zip(sizes, pngs):
        # width/height 0 means 256; no palette; 1 plane; 32 bits per pixel
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(struct.pack("<HHH", 0, 1, len(sizes)) + entries + b"".join(pngs))
    return out


def _pyinstaller_args(ui_dist: Path, out_dir: Path) -> list[str]:
    separator = ";" if sys.platform == "win32" else ":"
    icon = []
    if sys.platform == "win32":
        icon = ["--icon", str(windows_icon(ROOT / "build" / "icon.ico"))]
    return [
        *icon,
        str(PACKAGING / "launcher.py"),
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--distpath", str(out_dir),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build" / "pyinstaller"),
        # the backend: main.py and the qbx package, imported by the launcher at runtime
        "--paths", str(ENGINE),
        "--hidden-import", "main",
        "--collect-submodules", "qbx",
        # uvicorn picks its HTTP/loop implementations by name at runtime, which
        # PyInstaller's import scan can't see
        "--collect-submodules", "uvicorn",
        # the Claude Agent SDK (qbx/llm.py) is imported lazily, inside a function
        "--collect-submodules", "claude_agent_sdk",
        # ...and so is the Anthropic SDK (qbx/llm_api.py)
        "--collect-submodules", "anthropic",
        # keyring (qbx/api_key.py) finds its backends - Windows Credential Manager, the
        # macOS Keychain - through its package metadata's entry points, which a frozen app
        # only has if copied; without them it falls back to a backend that can't store
        "--collect-submodules", "keyring",
        "--copy-metadata", "keyring",
        *(["--collect-submodules", "win32ctypes"] if sys.platform == "win32" else []),
        # PyMuPDF 2.0 imports its layout model whenever it's installed (it comes with
        # pymupdf), and frozen without its ONNX files that import crashed the app at
        # start. The engine never asks for layout (page.get_layout), and pymupdf skips the
        # model on ImportError - so leave it, its runtime and pymupdf4llm out (~70 MB).
        "--exclude-module", "pymupdf.layout",
        "--exclude-module", "pymupdf4llm",
        "--exclude-module", "onnxruntime",
        "--add-data", f"{ui_dist}{separator}ui/dist",
        # data files the engine reads beside its code (the standard periodic table)
        "--add-data", f"{ENGINE / ENGINE_PACKAGE / 'assets'}{separator}{ENGINE_PACKAGE}/assets",
        # debug or release (qbx/edition.py)
        "--add-data", f"{edition_file()}{separator}{ENGINE_PACKAGE}",
        # the licence, notices and this version's complete source (qbx/legal.py, AGPL)
        "--add-data", f"{legal_folder()}{separator}{ENGINE_PACKAGE}/legal",
    ]


def _run(*cmd: str) -> None:
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)


def build_mac(ui_dist: Path, version: str, out_dir: Path = DIST) -> None:
    """"Classark Exam Builder.app" via PyInstaller, signed (hardened runtime, which
    notarisation requires), wrapped in a DMG with an Applications shortcut, notarised and
    stapled. Builds for this Mac's architecture: uv's Python is arm64 on Apple Silicon."""
    if sys.platform != "darwin":
        sys.exit("the mac build runs on a Mac - PyInstaller can't build for another system")
    import PyInstaller.__main__

    identity = os.environ.get("QBX_SIGN_IDENTITY")
    profile = os.environ.get("QBX_NOTARY_PROFILE")
    entitlements = PACKAGING / "entitlements.plist"
    args = [*_pyinstaller_args(ui_dist, out_dir), "--name", APP_NAME, "--osx-bundle-identifier", MAC_BUNDLE_ID]
    if identity:
        args += ["--codesign-identity", identity, "--osx-entitlements-file", str(entitlements)]
    PyInstaller.__main__.run(args)

    app = out_dir / f"{APP_NAME}.app"
    info_path = app / "Contents" / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update(
        CFBundleDisplayName=APP_NAME,
        CFBundleShortVersionString=version,
        CFBundleVersion=version,
        LSMinimumSystemVersion="11.0",
        NSHighResolutionCapable=True,
    )
    info_path.write_bytes(plistlib.dumps(info))
    if identity:
        # Info.plist changed after PyInstaller signed the bundle: sign the outside again
        _run("codesign", "--force", "--options", "runtime", "--timestamp",
             "--entitlements", str(entitlements), "--sign", identity, str(app))
        _run("codesign", "--verify", "--deep", "--strict", str(app))

    staging = ROOT / "build" / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    _run("ditto", str(app), str(staging / app.name))
    (staging / "Applications").symlink_to("/Applications")
    dmg = out_dir / f"{APP_NAME}-{version}.dmg"
    dmg.unlink(missing_ok=True)
    _run("hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(staging),
         "-ov", "-format", "UDZO", str(dmg))
    if not identity:
        print(f"wrote {dmg} - unsigned: set QBX_SIGN_IDENTITY to make one other Macs will open")
        return
    _run("codesign", "--force", "--timestamp", "--sign", identity, str(dmg))
    if not profile:
        print(f"wrote {dmg} - signed, not notarised: set QBX_NOTARY_PROFILE, or Gatekeeper will still warn")
        return
    _run("xcrun", "notarytool", "submit", str(dmg), "--keychain-profile", profile, "--wait")
    _run("xcrun", "stapler", "staple", str(dmg))
    _run("spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "-v", str(dmg))
    print(f"wrote {dmg} - signed and notarised, ready to send")


def main() -> None:
    global EDITION
    args = sys.argv[1:]
    if "--release" in args:
        args.remove("--release")
        EDITION = "release"
    out_dir = DIST
    if "--out" in args:
        at = args.index("--out")
        out_dir = Path(args[at + 1]).resolve()
        del args[at : at + 2]
    target = args[0] if args else "exe"
    if target not in ("exe", "mac", "zip", "installer"):
        sys.exit("usage: package.py [exe|mac|zip|installer] [--release] [--out <folder>]")
    print(f"edition: {EDITION}" + (" - Claude Code and Settings included, for testing" if EDITION == "debug" else ""))
    version = tomllib.loads((ENGINE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    ui_dist = build_ui()

    DIST.mkdir(exist_ok=True)
    if target in ("exe", "installer"):
        out_dir.mkdir(parents=True, exist_ok=True)
        build_exe(ui_dist, out_dir)
        if target == "installer":
            build_installers(version, out_dir)
        else:
            sign(out_dir / EXE_NAME / f"{EXE_NAME}.exe")
        return
    if target == "mac":
        out_dir.mkdir(parents=True, exist_ok=True)
        build_mac(ui_dist, version, out_dir)
        return
    out = DIST / f"{EXE_NAME}-{version}.zip"
    top = EXE_NAME
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ENGINE_FILES:
            zf.write(ENGINE / name, f"{top}/engine/{name}")
            count += 1
        for path in sorted(
            [*(ENGINE / ENGINE_PACKAGE).rglob("*.py"), *(ENGINE / ENGINE_PACKAGE / "assets").glob("*.pdf")]
        ):
            zf.write(path, f"{top}/engine/{path.relative_to(ENGINE).as_posix()}")
            count += 1
        zf.writestr(f"{top}/engine/{ENGINE_PACKAGE}/edition.txt", EDITION)
        count += 1
        for path in sorted(legal_folder().iterdir()):
            zf.write(path, f"{top}/engine/{ENGINE_PACKAGE}/legal/{path.name}")
            count += 1
        for path in sorted(p for p in ui_dist.rglob("*") if p.is_file()):
            zf.write(path, f"{top}/ui/dist/{path.relative_to(ui_dist).as_posix()}")
            count += 1
        # a .bat file needs Windows line endings to be parsed reliably, whatever the repo has
        launcher = (PACKAGING / "start.bat").read_text(encoding="utf-8").replace("\r\n", "\n")
        zf.writestr(f"{top}/start.bat", launcher.replace("\n", "\r\n"))
        readme = (PACKAGING / "README.txt").read_text(encoding="utf-8").replace("\r\n", "\n")
        zf.writestr(f"{top}/README.txt", readme.replace("\n", "\r\n"))
        count += 2

    size_kb = out.stat().st_size // 1024
    print(f"wrote {out.relative_to(ROOT)} ({count} files, {size_kb} KB)")


if __name__ == "__main__":
    main()

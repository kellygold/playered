#!/usr/bin/env python3
"""Build the Apple Silicon app from a pinned build environment; never publish it."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "PLAyered"


def run(*args: str, **kwargs):
    return subprocess.run(args, check=True, cwd=ROOT, **kwargs)


def collect_notices(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    # Copy exact installed license texts, not guessed SPDX mappings.
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name == "image23mf":
            continue
        entries = []
        for item in dist.files or []:
            if any(word in str(item).lower() for word in ("license", "copying", "copyright")):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    target = destination / "python-packages" / name / str(item).replace("../", "")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    entries.append(str(target.relative_to(destination)))
        inventory.append({"name": name, "version": dist.version, "notice_files": entries})
    for name in ("react", "react-dom", "scheduler"):
        source = ROOT / "frontend/node_modules" / name
        pkg = json.loads((source / "package.json").read_text())
        target = destination / "web" / name
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.is_file() and item.name.lower().startswith(("license", "copying")):
                shutil.copyfile(item, target / item.name)
        inventory.append({"name": name, "version": pkg["version"]})
    python_license = (
        Path(sys.base_prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "LICENSE.txt"
    )
    if not python_license.is_file():
        raise RuntimeError("Python runtime license not found; refusing to omit it")
    shutil.copyfile(python_license, destination / "Python-LICENSE.txt")
    shutil.copyfile(ROOT / "LICENSE", destination / "Image23MF-LICENSE.txt")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", destination / "NOTICE.txt")
    (destination / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-engine", action="store_true", help="Reuse only a locally built engine"
    )
    parser.add_argument(
        "--identity", default="-", help="Developer ID identity, or - for local ad-hoc build"
    )
    args = parser.parse_args()
    if sys.platform != "darwin" or os.uname().machine != "arm64" or sys.version_info[:2] != (3, 12):
        raise SystemExit("Build using Python 3.12 on an Apple Silicon Mac.")
    # Build Potrace as a separate executable; ship its complete unmodified source.
    vendor = ROOT / "packaging/macos/vendor"
    potrace_source = vendor / "potrace-1.16.tar.gz"
    if hashlib.sha256(potrace_source.read_bytes()).hexdigest() != (
        "be8248a17dedd6ccbaab2fcc45835bb0502d062e40fbded3bc56028ce5eb7acc"
    ):
        raise RuntimeError("Potrace source checksum mismatch")
    potrace_build = ROOT / "build/desktop-potrace"
    if potrace_build.exists():
        shutil.rmtree(potrace_build)
    potrace_build.mkdir(parents=True)
    with tarfile.open(potrace_source) as archive:
        archive.extractall(potrace_build, filter="data")
    potrace_dir = potrace_build / "potrace-1.16"
    compile_env = os.environ.copy()
    compile_env["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
    compile_env["CFLAGS"] = "-O2 -mmacosx-version-min=14.0"
    subprocess.run(
        ["./configure", "--disable-shared"], cwd=potrace_dir, env=compile_env, check=True
    )
    subprocess.run(
        ["make", "-j", str(os.cpu_count() or 2)], cwd=potrace_dir, env=compile_env, check=True
    )
    # Compile the exact vendored LGPL wrapper/C source shipped alongside the binary.
    vendor = ROOT / "packaging/macos/vendor"
    manifest = json.loads((vendor / "triangle-source.json").read_text())
    source_archive = vendor / "triangle-20250106-source.tar.gz"
    if hashlib.sha256(source_archive.read_bytes()).hexdigest() != manifest["source_sha256"]:
        raise RuntimeError("Triangle source archive checksum mismatch")
    source_dir = ROOT / "build/desktop-triangle"
    source_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source_archive) as archive:
        archive.extractall(source_dir, filter="data")
    run(
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(ROOT / "build/desktop-triangle-wheel"),
        str(source_dir / "triangle-20250106"),
    )
    wheels = list((ROOT / "build/desktop-triangle-wheel").glob("triangle-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one source-built Triangle wheel")
    run(sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheels[0]))
    output = ROOT / "dist/macos"
    output.mkdir(parents=True, exist_ok=True)
    app = output / f"{NAME}.app"
    subprocess.run(["npm", "run", "build"], cwd=ROOT / "frontend", check=True)
    if not args.skip_engine:
        run(
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(output / "frozen"),
            "--workpath",
            str(ROOT / "build/desktop-freeze"),
            "packaging/macos/desktop-engine.spec",
        )
    frozen = output / "frozen/image23mf-engine"
    run(
        str(frozen / "image23mf-engine"),
        "--self-test",
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )
    if app.exists():
        shutil.rmtree(app)
    resources = app / "Contents/Resources"
    executable = app / "Contents/MacOS/Studio"
    executable.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copytree(frozen, resources / "engine", symlinks=True)
    (resources / "tools").mkdir()
    shutil.copy2(potrace_dir / "src/potrace", resources / "tools/potrace")
    run(str(resources / "tools/potrace"), "--version")
    shutil.copytree(ROOT / "frontend/dist", resources / "web")
    iconset = ROOT / "build/Studio.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            suffix = "@2x" if scale == 2 else ""
            run(
                "sips",
                "-z",
                str(size * scale),
                str(size * scale),
                "packaging/macos/desktop/icon-source.png",
                "--out",
                str(iconset / f"icon_{size}x{size}{suffix}.png"),
                stdout=subprocess.DEVNULL,
            )
    run("iconutil", "-c", "icns", str(iconset), "-o", str(resources / "Studio.icns"))
    collect_notices(resources / "LICENSES")
    shutil.copytree(vendor, resources / "LICENSES/sources-and-notices")
    env = os.environ.copy()
    # Match compiler and SDK; do not change the machine-wide xcode-select setting.
    if Path("/Applications/Xcode.app/Contents/Developer").is_dir():
        env.setdefault("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")
    run(
        "xcrun",
        "swiftc",
        "packaging/macos/desktop/main.swift",
        "-O",
        "-o",
        str(executable),
        "-target",
        "arm64-apple-macos14.0",
        "-framework",
        "AppKit",
        "-framework",
        "WebKit",
        "-framework",
        "Security",
        env=env,
    )
    from image23mf import __version__

    info = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleExecutable": "Studio",
        "CFBundleIdentifier": "tools.burner.image23mf",
        "CFBundleIconFile": "Studio",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Burner Tools",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSDocumentsFolderUsageDescription": (
            "Keep your images, projects and exports in your chosen workspace."
        ),
        "NSDownloadsFolderUsageDescription": "Save the files you export.",
    }
    with (app / "Contents/Info.plist").open("wb") as file:
        plistlib.dump(info, file)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (resources / "build-info.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "python": sys.version,
                "version": __version__,
                "architecture": "arm64",
                "source_dirty": bool(
                    subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
                ),
                "minimum_macos": "14.0",
            },
            indent=2,
        )
        + "\n"
    )
    # Sign native libraries before their containing executable and bundle.
    binaries = []
    for item in resources.rglob("*"):
        if item.is_file() and not item.is_symlink():
            with item.open("rb") as stream:
                magic = stream.read(4)
            if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
                binaries.append(item)
    timestamp = ["--timestamp"] if args.identity != "-" else []
    hardened = ["--options", "runtime"] if args.identity != "-" else []
    for binary in sorted(binaries, key=lambda p: len(p.parts), reverse=True):
        run("codesign", "--force", *hardened, "--sign", args.identity, *timestamp, str(binary))
    run("codesign", "--force", *hardened, "--sign", args.identity, *timestamp, str(app))
    run("codesign", "--verify", "--deep", "--strict", str(app))
    archive = output / "PLAyered-0.1.0-arm64.zip"
    archive.unlink(missing_ok=True)
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive))
    (output / "SHA256SUMS").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n"
    )
    print(
        f"Built {app}\nArchive: {archive}\nSigning identity: {args.identity}\n"
        "Notarization and release validation are separate gates."
    )


if __name__ == "__main__":
    main()

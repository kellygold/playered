# Third-party dependencies

The repository's MIT license covers Burner Tools' code and original procedural
fixtures. Dependencies and separately installed tools retain their own licenses.
Source setup installs Python/npm dependencies from their package registries.
The desktop app bundles Python and its runtime libraries plus prebuilt React assets.
Their exact installed notices and a version inventory are inside Contents/Resources/LICENSES.
Bambu Studio and OpenSCAD remain separately installed tools. Potrace is bundled
as a separate command-line executable in the desktop package.

## Triangle

`triangle==20250106` is used to triangulate flat regions, including holes, before
extruding the printable mesh. The Python wrapper identifies its license as
LGPL-3.0. Its underlying Jonathan Richard Shewchuk Triangle C implementation has
separate conditions; the wrapper's license is not a replacement for those terms.

- [Python wrapper](https://github.com/drufat/triangle)
- [Wrapper license](https://github.com/drufat/triangle/blob/master/LICENSE)
- [Triangle C copyright and redistribution conditions](https://github.com/drufat/triangle-c/blob/master/triangle.c)
- [Original author's distribution page](https://www.cs.cmu.edu/~quake/triangle.html)

The upstream C terms permit redistribution without compensation while retaining
copyright notices. Modified versions carry additional notice/source obligations.
Distribution as part of a commercial system requires arrangement with the author;
the header separately addresses directing users to obtain the library themselves.
Check those actual terms before bundling or selling a distribution. A free app or
an attribution notice does not replace the dependency's conditions. Triangle is not MIT licensed. Source setup installs it separately; the free desktop
beta builds the exact vendored wrapper and C source and includes both the corresponding
source archive and all original notices in its app bundle.

The desktop build uses wrapper tag v20250106, commit
595b43eb6682992a0b1012b9671bf860c0e6ae56, and its pinned Triangle C commit
8b9e1046e5cddab1298d3204f10c93665836cf99. Both are unmodified. Source and hashes are
in packaging/macos/vendor and included in the app. The archive contains build
instructions (setup.py/pyproject.toml), the LGPL text and original C copyright header.
The desktop build script compiles that source; it does not substitute a registry wheel.

You may rebuild/modify these library components under their own terms and debug
those modifications. No Image23MF restriction forbids that. A modified app must be
re-signed locally after replacing library files; Apple's code signature covers their
bytes. The included source/build scripts let you rebuild the full app with local
ad-hoc signing. This notice does not grant commercial redistribution rights to
Triangle; the desktop beta is distributed free of charge.

## Other runtime dependencies

FastAPI, NumPy, Pillow, Pydantic, pydantic-settings, Uvicorn, React and React DOM
are installed as dependencies rather than copied into this source tree. Their
copyright/license notices accompany their distributions. Build and development
dependencies are listed in `pyproject.toml` and `frontend/package-lock.json`.

Bambu Studio is a separately installed application required for the supported
validated export flow. Its availability does not grant this project redistribution
rights over Bambu Studio or its profile assets. Do not copy those assets into a
public release package.

## Potrace

The desktop package includes unmodified Potrace 1.16 by Peter Selinger under
GPL-2.0-or-later, invoked as a separate command-line tool. Its complete upstream
source archive, including COPYING and build scripts, is shipped beside the
Triangle source under Contents/Resources/LICENSES/sources-and-notices. The original
archive is https://potrace.sourceforge.net/download/1.16/potrace-1.16.tar.gz,
SHA256 be8248a17dedd6ccbaab2fcc45835bb0502d062e40fbded3bc56028ce5eb7acc.
The public desktop build script records its configure/make commands.

## Bundled Python runtime

The desktop runtime is CPython 3.12.13 from python-build-standalone release
20260728 (aarch64-apple-darwin). Its upstream PYTHON.json records library license
mappings; the complete release license directory accompanies the app under
LICENSES/sources-and-notices/python-3.12.13-notices, including the notices for
statically linked libraries. Provenance and download checksums accompany them.

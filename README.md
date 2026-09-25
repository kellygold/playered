# PLAyered

A free Mac app from Burner Tools for turning an image into a multicolor
3MF. Choose a palette, clean up small details, inspect the preview and build a
project that can be opened in Bambu Studio. Images and saved projects stay on
your computer during the core workflow.

**For Apple Silicon Macs.** The desktop edition packages the existing interface and
processing engine in a standalone Mac window. It needs no Python, Node.js or
Terminal. Bambu Studio remains a separate prerequisite.

See [Mac installation and builds](docs/desktop-install.md). Source development
instructions follow below.

Download the signed Mac app at [playered.art](https://playered.art/).

## Source development requirements

- macOS; other operating systems have not been validated.
- Python 3.12 recommended (the code supports Python 3.9 or newer).
- Node.js 22.14 or newer and npm.
- Bambu Studio 2.8 or newer installed for validated 3MF export. The currently supported preset
  combinations are Bambu P2S, 0.2/0.4 mm nozzles, Textured PEI Plate, and the bundled
  Bambu PLA Basic/Matte profiles. Other printers/profiles are not promised.
- Potrace 1.16 is required for vectorization when running from source. The desktop
  package bundles it. OpenSCAD is optional for reference benchmarks.

## Run from source

From a checkout of this repository:

```sh
make bootstrap
```

Then run these in two terminals:

```sh
make api
```

```sh
make web
```

Open **http://127.0.0.1:5173**. The browser connects to the local service on port
8323. Stop both terminal processes when finished. The API must remain on loopback;
it is not designed as an authenticated shared internet service.

Use **Try guided sample** for a first run, or import PNG, JPEG or WebP. Choose your
physical dimensions and palette, inspect the processed preview, then select
**Build 3MF**. Export runs geometry checks and Bambu Studio slice validation before
the download becomes available. Large or noisy photos can take several minutes.

The optional source-built launcher described in [local launch](docs/macos-local-app.md)
is a convenience for existing Mac users; it is not a signed, portable app download.

## Saved work and limits

The development workspace lives in ignored `workspace/`. The app saves projects
locally and supports project/workspace backup. Keep backups before upgrades or
moving data; see [workspace backup](docs/workspace-backup-restore.md).

- Fine image detail can be smaller than a nozzle can print. Preview warnings and
  successful slicing help assess a result, but do not guarantee a physical print.
- Users have printed output from this pipeline. This beta does not claim systematic
  certification of every printer, filament or nozzle combination.
- Complex previews can produce large reports and consume substantial memory.
- Core image processing is local. Optional provider integrations have separate
  consent and configuration boundaries; no API key is required for the core app.

## Development

```sh
make check    # lint, Python/frontend tests, wheel and frontend build
```

Hosted CI runs the software checks. Real Bambu export and browser release journeys
are documented in [release checks](docs/release-gate.md); unavailable external tools
must be reported as untested, not counted as successful checks.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and test guidance and
[SECURITY.md](SECURITY.md) for responsible reporting. Technical documentation lives
under `docs/`; historical planning links are not required to contribute.

## License

Original code and procedural test artwork are MIT licensed. Dependencies keep
their own terms, including Triangle's separate conditions. Read
[LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before
redistributing a packaged application.

## Support

If Image23MF is useful to you, you can [support Burner Tools](https://buymeacoffee.com/burnertools)
on Buy Me a Coffee.

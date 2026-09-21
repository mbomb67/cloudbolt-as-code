# Contributing

Changes land through pull requests to `main`. Every PR must pass CI and get a code-owner review.

## Before opening a PR

- Follow the layout and rules in [AGENTS.md](AGENTS.md). Content lives in ID-prefixed folders with a colocated `<ID>_metadata.json`; cross-references are `"<dir>/<ID>"` strings.
- Replace anything instance-specific with `FILL-ME` or a `<placeholder>`: credentials, subscription and tenant IDs, ConnectionInfo and handler IDs, hostnames, DNS zones, customer names. Plugins should refuse to run while a `FILL-ME` value is present.
- Declare every shared module a plugin imports under `dependencies.sharedModules`, or it will not sync with the plugin.
- Add or update the folder's `README.md`: what it does, prerequisites, setup, caveats. Keep it short.
- Keep the metadata `description` to one sentence saying what the content does. The catalog prints it verbatim.
- Regenerate the catalog after adding, renaming, or re-describing content: `python tools/build_catalog.py`. `CATALOG.md`, `catalog.json`, and each directory's `README.md` are generated from metadata; do not edit them by hand.
- Run the checks locally:

  ```bash
  python tools/validate_metadata.py
  python tools/build_catalog.py --check
  python -m compileall -q plugins shared_modules extensions
  ```

## What CI checks

| Check | Fails the build |
|---|---|
| Metadata lint (`tools/validate_metadata.py`) | yes |
| Catalog freshness (`tools/build_catalog.py --check`) | yes |
| Python compile | yes |
| Secret scan (gitleaks, rules in `.gitleaks.toml`) | yes |
| Bandit static scan | no, advisory |
| CodeQL (Python, JavaScript) | reported under Security |

## License

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).

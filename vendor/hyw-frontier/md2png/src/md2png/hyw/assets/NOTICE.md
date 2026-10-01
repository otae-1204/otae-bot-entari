# HYw artwork provenance

- `icons.json`: six unmodified vector paths from the pinned `@iconify-json/mdi` 1.2.3 dataset (Pictogrammers Material Design Icons, Apache 2.0). Source archive SHA-256: `ddd817a7213f91de51147943915c0cda8c8b818a53c92b5c068cfa9883c6d7ac`. See `Apache-2.0.txt`.
- `KaTeX_*.ttf`: unmodified embedded font binaries from the HYw working-tree snapshot; SIL OFL 1.1, see `OFL.txt` and each font's name table. These are preparation assets for future formula parity, **not used by the current Ziamath renderer**.
- `math-metrics.json`: KaTeX font metric data extracted from that same snapshot, see `KaTeX-MIT.txt`.
- `provenance.json`: source and font hashes; `scripts/prepare_hyw_assets.py` reproduces extraction. No browser screenshots or component bitmap crops are packaged.
- macOS SF NS, PingFang SC and Menlo are read from the user's operating system; their binaries are not redistributed.

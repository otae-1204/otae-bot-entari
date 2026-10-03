# Bundled reading fonts

The renderer uses these fonts offline; it does not download fonts while rendering.

- `NotoSansSC.ttf` and `NotoSansMono.ttf`: existing bundled fonts; see their accompanying OFL notices.
- `NotoSansKR.ttf`: unmodified Google Fonts `ofl/notosanskr/NotoSansKR[wght].ttf`, retrieved 2026-09-10. Copyright 2014–2021 Adobe; SIL OFL 1.1, reserved name “Source”. See `notosanskr-OFL.txt`.
  - Source: https://github.com/google/fonts/tree/main/ofl/notosanskr
  - SHA-256: `194018e6b2b293a7964f037b25c0249ce1418bc9ab3c971060a03aa57861e252`
- `NotoEmoji.ttf`: unmodified Google Fonts `ofl/notoemoji/NotoEmoji[wght].ttf`, retrieved 2026-09-10. Copyright 2013 Google LLC; SIL OFL 1.1. See `notoemoji-OFL.txt`.
  - Source: https://github.com/google/fonts/tree/main/ofl/notoemoji
  - SHA-256: `de6c18832938afc99caf132b39d6a30a19bac7f2e812e28db2535b4608d27551`
- `BabelStoneHan.ttf`: unmodified BabelStone Han 16.0.3 (2025-01-01), retrieved 2026-09-10 from the versioned upstream ZIP. Copyright 1994–1999 Arphic Technology Co., Ltd.; copyright 2009–2025 Andrew West. Distributed under the original Arphic Public License, which permits personal and commercial redistribution. `ARPHICPL.TXT` reproduces the license text embedded in the font's name table.
  - Source: https://www.babelstone.co.uk/Fonts/Han.html
  - Archive: https://www.babelstone.co.uk/Fonts/Download/BabelStoneHan-16.0.3.zip
  - SHA-256: `d8bb747b3fdccd84a60bd0aa56bb90937270d0bd15f1101cd8f2a5a3709dd0a3`

The former generated `NotoSansSC-Regular.ttf` is no longer needed: formula fallback uses the variable font outlines at the requested weight through FontTools. The math engine's STIX Two Math font is supplied by the Ziamath dependency, not copied here; its embedded notice specifies SIL OFL 1.1.

Emoji are monochrome outline glyphs. The fallback fonts do not cover all Unicode. In the HYw renderer, unsupported graphemes are shown as explicit Unicode labels (in the form `[U+…]`) with a `missing-glyph` diagnostic, rather than silently painted as whitespace.

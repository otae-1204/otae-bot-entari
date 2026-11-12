from pathlib import Path
import unittest

from scripts.endfield.extract_endfield_ui_bundles import (
    ALL_ICON_PATTERNS,
    bundle_file_matches,
    chacha20_xor,
    load_bundle_entries,
    safe_relative_bundle_path,
    select_assets,
)


class EndfieldUiBundleTests(unittest.TestCase):
    def test_chacha20_matches_rfc_8439_block_vector(self) -> None:
        key = bytes(range(32))
        nonce = bytes.fromhex("000000090000004a00000000")
        expected = bytes.fromhex(
            "10f1e7e4d13b5915500fdd1fa32071c4"
            "c7d1f4c733c068030422aa9ac3d46c4e"
            "d2826446079faa0914c2d705d98b02a2"
            "b5129cd1de164eb9cbd083e8a2503c4e"
        )

        self.assertEqual(chacha20_xor(bytes(64), key, nonce, counter=1), expected)

    def test_bundle_path_rejects_traversal(self) -> None:
        for bundle_name in ("../escape.ab", "/root.ab", "a/../../b.ab"):
            with self.subTest(bundle_name=bundle_name):
                with self.assertRaises(ValueError):
                    safe_relative_bundle_path(bundle_name)

    def test_bundle_path_preserves_manifest_layout(self) -> None:
        self.assertEqual(
            safe_relative_bundle_path("main/abc.ab"), Path("main", "abc.ab")
        )

    def test_existing_bundle_requires_matching_md5(self) -> None:
        import hashlib
        import tempfile

        payload = b"current bundle"
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory, "bundle.ab")
            bundle.write_bytes(payload)
            entry = {
                "Len": len(payload),
                "FileDataMD5": hashlib.md5(payload).hexdigest(),
            }
            self.assertTrue(bundle_file_matches(bundle, entry))

            bundle.write_bytes(b"obsolete bytes")
            self.assertFalse(bundle_file_matches(bundle, entry))

    def test_multiple_bundle_indexes_keep_group_and_version(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            files = []
            for name, group, version in (
                ("main/a.ab", "7064D8E2", 3),
                ("initial/b.ab", "0CE8FA57", 4),
            ):
                path = Path(directory, f"{group}.json")
                path.write_text(
                    json.dumps(
                        {
                            "Version": version,
                            "GroupCfgHashName": group,
                            "AllChunks": [
                                {
                                    "Files": [
                                        {
                                            "FileName": f"Data/Bundles/Windows/{name}",
                                            "FileChunkMD5Name": "chunk",
                                        }
                                    ]
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                files.append(path)

            entries = load_bundle_entries(files)

        self.assertEqual(entries["main/a.ab"]["_vfs_group_hash"], "7064D8E2")
        self.assertEqual(entries["initial/b.ab"]["_version"], 4)

    def test_all_icons_preset_selects_files_inside_icon_directories(self) -> None:
        import csv
        import tempfile

        rows = (
            (
                "assets/beyond/dynamicassets/gameplay/ui/sprites/itemicon/item_ore.png",
                "item.ab",
            ),
            (
                "assets/beyond/arts/ui/sprites/charinfo/potential/potential_arc.png",
                "potential.ab",
            ),
            (
                "assets/beyond/arts/ui/sprites/common/background.png",
                "background.ab",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory, "manifest.csv")
            with manifest.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=("asset_path", "bundle_name", "bundle_index", "asset_size"),
                )
                writer.writeheader()
                for asset_path, bundle_name in rows:
                    writer.writerow(
                        {
                            "asset_path": asset_path,
                            "bundle_name": bundle_name,
                            "bundle_index": "0",
                            "asset_size": "1",
                        }
                    )

            selected, bundles = select_assets(manifest, ALL_ICON_PATTERNS)

        self.assertEqual(
            [row["asset_path"] for row in selected],
            [rows[0][0], rows[1][0]],
        )
        self.assertEqual(bundles, {"item.ab", "potential.ab"})


if __name__ == "__main__":
    unittest.main()

"""插件构造媒体元素只经 otae_bot.messaging.media：字节进、data: 出，不落临时文件。"""

from __future__ import annotations

import base64
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from otae_bot.messaging import media

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(20)


class MediaHelperTests(unittest.TestCase):
    def test_png_and_image_are_inline_data(self):
        self.assertEqual(str(media.png(PNG)), f'<img src="data:image/png;base64,{base64.b64encode(PNG).decode()}"/>')
        self.assertTrue(media.image(JPEG, "image/jpeg").src.startswith("data:image/jpeg;base64,"))
        self.assertTrue(media.image(PNG).src.startswith("data:image/png;base64,"))
        # 认不出类型时按 PNG 标注，交给平台嗅探
        self.assertTrue(media.image(b"not an image").src.startswith("data:image/png;base64,"))

    def test_file_keeps_its_name_and_type(self):
        element = media.file(b"hello", "notes.txt")
        self.assertEqual(element.title, "notes.txt")
        self.assertTrue(element.src.startswith("data:text/plain;base64,"))

    def test_image_file_reads_bytes_and_caches_until_the_file_changes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "help.png"
            path.write_bytes(PNG)
            first = media.image_file(path)
            self.assertNotIn("file:", first.src)
            self.assertEqual(base64.b64decode(first.src.partition(",")[2]), PNG)
            self.assertIs(media.read_asset(path), media.read_asset(path))
            path.write_bytes(PNG + b"v2")
            os.utime(path, ns=(1, 2))
            self.assertEqual(media.read_asset(path), PNG + b"v2")


if __name__ == "__main__":
    unittest.main()

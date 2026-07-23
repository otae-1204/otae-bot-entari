from pathlib import Path
import tempfile
import unittest

from PIL import Image

from plugins.endfield import draw
from scripts.build_endfield_zero_potential_icon import build_zero_potential_icon


class EndfieldPotentialIconTests(unittest.TestCase):
    def test_zero_level_uses_zero_potential_asset(self) -> None:
        markup = draw.potential_star("0")

        self.assertIn("potential-star-p0", markup)
        self.assertIn("data:image/png;base64,", markup)
        self.assertIn('alt="P0"', markup)

    def test_invalid_and_negative_levels_use_zero_state(self) -> None:
        self.assertIn("potential-star-p0", draw.potential_star("invalid"))
        self.assertIn("potential-star-p0", draw.potential_star("-1"))

    def test_builder_matches_other_potential_inactive_cell_color(self) -> None:
        source = Path("assets/image/endfield/potential/wpn_potential_01.png")
        max_source = source.with_name("wpn_potential_05.png")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "wpn_potential_00.png")
            build_zero_potential_icon(source, output)
            image = Image.open(output).convert("RGBA")
            max_image = Image.open(max_source).convert("RGBA")

        yellow_pixels = 0
        white_pixels = 0
        gray_inactive_pixels = 0
        black_inactive_pixels = 0
        for output_pixel, max_pixel in zip(image.getdata(), max_image.getdata()):
            red, green, blue, alpha = output_pixel
            if not alpha:
                continue
            if red > 180 and green > 100 and blue < 100:
                yellow_pixels += 1
            if min(red, green, blue) > 180 and max(red, green, blue) - min(
                red, green, blue
            ) < 35:
                white_pixels += 1
            max_red, max_green, max_blue, max_alpha = max_pixel
            max_cell = (
                max_alpha
                and min(max_red, max_green, max_blue) > 115
                and max(max_red, max_green, max_blue)
                - min(max_red, max_green, max_blue)
                < 35
            )
            if max_cell:
                if (red, green, blue, alpha) == (92, 92, 92, 81):
                    gray_inactive_pixels += 1
                if max(red, green, blue) < 35 and alpha > 180:
                    black_inactive_pixels += 1

        self.assertGreater(yellow_pixels, 500)
        self.assertGreater(gray_inactive_pixels, 1000)
        self.assertLess(black_inactive_pixels, 100)
        self.assertEqual(white_pixels, 0)


if __name__ == "__main__":
    unittest.main()

from io import BytesIO
from unittest import IsolatedAsyncioTestCase, TestCase, mock

from PIL import Image

from otae_bot.attendance_registry import FAILED, SIGNED, AttendanceOutcome
from plugins.signin import presentation


def png(color, size=(80, 40), mode="RGB"):
    out = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(out, "PNG")
    return out.getvalue()


class CardStackTests(TestCase):
    def test_both_results_retain_their_pixels_and_order(self):
        result = presentation.stack_cards([png("red"), png("blue", (80, 60))])
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.size, (80, 100))
            self.assertEqual(image.getpixel((40, 39)), (255, 0, 0))
            self.assertEqual(image.getpixel((40, 40)), (0, 0, 255))
            self.assertEqual(image.getpixel((40, 99)), (0, 0, 255))

    def test_different_widths_are_scaled_proportionally_without_upscaling(self):
        result = presentation.stack_cards([png("red", (160, 80)), png("blue")])
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.size, (80, 80))

    def test_single_game_keeps_the_original_bytes(self):
        card = png("red")
        self.assertEqual(presentation.stack_cards([card]), card)

    def test_oversized_combination_is_rejected_instead_of_cutting_rows(self):
        with mock.patch.object(presentation, "MAX_IMAGE_PIXELS", 5000), self.assertRaises(ValueError):
            presentation.stack_cards([png("red"), png("blue")])


class DeliveryTests(IsolatedAsyncioTestCase):
    async def test_one_game_failed_preserves_the_other_image_and_failure_text(self):
        card = png("blue")
        result = await presentation.build_delivery([
            AttendanceOutcome("endfield", "终末地", FAILED, "终末地：登录过期"),
            AttendanceOutcome("arknights", "明日方舟", SIGNED, "明日方舟：已签到", card),
        ])
        self.assertEqual(result.png, card)
        self.assertEqual(result.text, "终末地：登录过期")
        self.assertIn("明日方舟：已签到", result.fallback_text)

    async def test_invalid_image_keeps_both_complete_text_reports(self):
        result = await presentation.build_delivery([
            AttendanceOutcome("endfield", "终末地", SIGNED, "终末地：角色甲成功", b"broken"),
            AttendanceOutcome("arknights", "明日方舟", SIGNED, "明日方舟：角色乙失败", png("blue")),
        ])
        self.assertIsNone(result.png)
        self.assertEqual(result.text, "终末地：角色甲成功\n\n明日方舟：角色乙失败")

"""Stacking and sending the attendance cards of ``/ak 签到`` and ``/ef 签到``."""

from io import BytesIO
from unittest import IsolatedAsyncioTestCase, TestCase, mock

from arclet.letoderea.exceptions import _ExitException
from PIL import Image

from otae_bot import attendance_delivery as delivery_module
from otae_bot.attendance_registry import FAILED, SIGNED, AttendanceOutcome


def png(color, size=(80, 40), mode="RGB"):
    out = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(out, "PNG")
    return out.getvalue()


class CardStackTests(TestCase):
    def test_both_results_retain_their_pixels_and_order(self):
        result = delivery_module.stack_cards([png("red"), png("blue", (80, 60))])
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.size, (80, 100))
            self.assertEqual(image.getpixel((40, 39)), (255, 0, 0))
            self.assertEqual(image.getpixel((40, 40)), (0, 0, 255))
            self.assertEqual(image.getpixel((40, 99)), (0, 0, 255))

    def test_different_widths_are_scaled_proportionally_without_upscaling(self):
        result = delivery_module.stack_cards([png("red", (160, 80)), png("blue")])
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.size, (80, 80))

    def test_single_game_keeps_the_original_bytes(self):
        card = png("red")
        self.assertEqual(delivery_module.stack_cards([card]), card)

    def test_oversized_combination_is_rejected_instead_of_cutting_rows(self):
        with mock.patch.object(delivery_module, "MAX_IMAGE_PIXELS", 5000), self.assertRaises(ValueError):
            delivery_module.stack_cards([png("red"), png("blue")])


class BuildDeliveryTests(IsolatedAsyncioTestCase):
    async def test_one_game_failed_preserves_the_other_image_and_failure_text(self):
        card = png("blue")
        result = await delivery_module.build_delivery([
            AttendanceOutcome("arknights", "明日方舟", SIGNED, "明日方舟：已签到", card),
            AttendanceOutcome("endfield", "终末地", FAILED, "终末地：登录过期"),
        ])
        self.assertEqual(result.png, card)
        self.assertEqual(result.text, "终末地：登录过期")
        self.assertIn("明日方舟：已签到", result.fallback_text)
        self.assertIn("终末地：登录过期", result.fallback_text)

    async def test_the_requested_game_stays_on_top(self):
        top, bottom = png("red"), png("blue")
        result = await delivery_module.build_delivery([
            AttendanceOutcome("arknights", "明日方舟", SIGNED, "明日方舟", top),
            AttendanceOutcome("endfield", "终末地", SIGNED, "终末地", bottom),
        ])
        with Image.open(BytesIO(result.png)) as image:
            self.assertEqual(image.size, (80, 80))
            self.assertEqual(image.getpixel((40, 10)), (255, 0, 0))
            self.assertEqual(image.getpixel((40, 70)), (0, 0, 255))
        self.assertEqual(result.text, "")
        self.assertEqual(result.fallback_text, "明日方舟\n\n终末地")

    async def test_invalid_image_keeps_both_complete_text_reports(self):
        result = await delivery_module.build_delivery([
            AttendanceOutcome("endfield", "终末地", SIGNED, "终末地：角色甲成功", b"broken"),
            AttendanceOutcome("arknights", "明日方舟", SIGNED, "明日方舟：角色乙失败", png("blue")),
        ])
        self.assertIsNone(result.png)
        self.assertEqual(result.text, "终末地：角色甲成功\n\n明日方舟：角色乙失败")

    async def test_a_single_card_is_sent_without_recomposition(self):
        card = b"\x89PNG-card"
        with mock.patch.object(delivery_module, "run_image_render") as render:
            result = await delivery_module.build_delivery([
                AttendanceOutcome("arknights", "明日方舟", SIGNED, "完整文字结果", card),
            ])
        render.assert_not_called()
        self.assertEqual((result.png, result.text, result.fallback_text), (card, "", "完整文字结果"))


class DeliverTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(delivery_module, "png_image", lambda data: f"<image {len(data)}>"))
        self.sent: list = []
        self.failures = 0

    async def send(self, message):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("message connection interrupted")
        self.sent.append(message)

    async def test_image_and_notice_go_out_in_one_message(self):
        delivery = delivery_module.AttendanceDelivery(b"png", "终末地：签到失败", "完整文字")
        await delivery_module.deliver(self.send, delivery)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIsInstance(self.sent[0], str)
        self.assertIn("终末地：签到失败", str(self.sent[0]))

    async def test_a_failed_image_send_is_retried_once_as_the_full_text(self):
        self.failures = 1
        delivery = delivery_module.AttendanceDelivery(b"png", "", "明日方舟：签到成功\n\n终末地：今日已签到")
        await delivery_module.deliver(self.send, delivery)
        self.assertEqual(self.sent, ["明日方舟：签到成功\n\n终末地：今日已签到"])

    async def test_a_failed_text_retry_is_only_logged(self):
        self.failures = 2
        await delivery_module.deliver(self.send, delivery_module.AttendanceDelivery(b"png", "", "完整文字"))
        self.assertEqual(self.sent, [])

    async def test_a_text_only_result_is_not_sent_twice(self):
        self.failures = 1
        await delivery_module.deliver(self.send, delivery_module.AttendanceDelivery(None, "完整文字", "完整文字"))
        self.assertEqual(self.sent, [])

    async def test_entari_exit_still_propagates(self):
        async def stop(_message):
            raise _ExitException("stop")

        with self.assertRaises(_ExitException):
            await delivery_module.deliver(stop, delivery_module.AttendanceDelivery(b"png", "", "完整文字"))

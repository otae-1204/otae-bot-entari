import cv2
from PIL import Image, ImageFile, ImageDraw, ImageFont, ImageFilter
from typing import Tuple, Optional, Union, List, Literal
from configs.path_config import IMAGE_PATH, FONT_PATH, TEMP_PATH
from configs.config import SYSTEM_PROXY
from io import BytesIO
import base64
from pathlib import Path
import numpy as np
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor


_playwright_manager = None
_browser = None
_browser_lock = threading.Lock()
_browser_executor = None
_browser_executor_lock = threading.Lock()


def _playwright_proxy_conf():
    if isinstance(SYSTEM_PROXY, dict) and SYSTEM_PROXY.get("http"):
        proxy_url = SYSTEM_PROXY["http"].rstrip("/")
        if "://" not in proxy_url:
            proxy_url = f"http://{proxy_url}"
        return {"server": proxy_url}
    return None


def _get_browser_executor():
    global _browser_executor
    with _browser_executor_lock:
        if _browser_executor is None:
            _browser_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="playwright",
            )
        return _browser_executor


def _get_browser():
    """Return a reusable sync Playwright browser instance."""
    global _playwright_manager, _browser
    with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser

        from playwright.sync_api import sync_playwright

        _playwright_manager = sync_playwright().start()
        _browser = _launch_browser(_playwright_manager, _playwright_proxy_conf())
        return _browser


async def close_browser():
    """Close the reusable browser, useful for tests or graceful shutdown."""
    global _browser_executor
    executor = _get_browser_executor()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, _close_browser_sync)
    with _browser_executor_lock:
        if _browser_executor is executor:
            executor.shutdown(wait=False, cancel_futures=True)
            _browser_executor = None


def _close_browser_sync():
    global _playwright_manager, _browser
    with _browser_lock:
        if _browser is not None:
            _browser.close()
            _browser = None
        if _playwright_manager is not None:
            _playwright_manager.stop()
            _playwright_manager = None


async def screenshot_web_element(
    web_url: str,
    selector: str = "body",
    *,
    viewport: tuple[int, int] = (1280, 900),
    timeout_ms: int = 15000,
    max_height: int = 20000,
) -> bytes:
    """Screenshot a single page element using a reusable browser."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _get_browser_executor(),
        _screenshot_web_element_sync,
        web_url,
        selector,
        viewport,
        timeout_ms,
        max_height,
    )


def _screenshot_web_element_sync(
    web_url: str,
    selector: str,
    viewport: tuple[int, int],
    timeout_ms: int,
    max_height: int,
) -> bytes:
    browser = _get_browser()
    context = browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]},
        device_scale_factor=1,
    )
    page = context.new_page()
    try:
        def _route_handler(route):
            if route.request.resource_type in {"media", "font"}:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", _route_handler)
        page.goto(web_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            locator = page.locator(selector).first
        except Exception:
            page.wait_for_selector("body", timeout=timeout_ms)
            locator = page.locator("body").first

        size = locator.evaluate(
            """el => ({
                width: Math.ceil(Math.max(
                    el.scrollWidth,
                    el.offsetWidth,
                    el.getBoundingClientRect().width
                )),
                height: Math.ceil(Math.max(
                    el.scrollHeight,
                    el.offsetHeight,
                    el.getBoundingClientRect().height
                ))
            })"""
        )
        target_width = max(viewport[0], min(max(1, int(size["width"])), 4000))
        target_height = max(viewport[1], min(max(1, int(size["height"])), max_height))
        page.set_viewport_size({"width": target_width, "height": target_height})
        page.wait_for_timeout(300)

        box = locator.bounding_box()
        if not box:
            return page.screenshot(type="png", full_page=True)

        return page.screenshot(
            clip={
                "x": max(0, box["x"]),
                "y": max(0, box["y"]),
                "width": max(1, min(box["width"], target_width - max(0, box["x"]))),
                "height": max(1, min(box["height"], target_height)),
            },
            type="png",
        )
    finally:
        context.close()


def _launch_browser(playwright, proxy_conf):
    """Try system Chrome/Edge first, fall back to bundled Chromium."""
    for channel in ["chrome", "msedge", None]:
        try:
            kwargs = {"headless": True}
            if channel:
                kwargs["channel"] = channel
            if proxy_conf:
                kwargs["proxy"] = proxy_conf
            return playwright.chromium.launch(**kwargs)
        except Exception:
            continue
    raise RuntimeError("No browser found. Install Chrome/Edge or run: playwright install chromium")


async def WebImageBuilders(fill_name: str = None, web_url: str = None, **kwargs):
    """截图指定网页并保存（使用系统 Chrome/Edge，无需额外安装浏览器）."""
    fill_name = fill_name or kwargs.get("fillName")
    web_url = web_url or kwargs.get("webUrl")
    if not fill_name or not web_url:
        raise TypeError("WebImageBuilders requires fill_name/web_url")

    png = await screenshot_web_element(web_url, "body")
    Path(TEMP_PATH).mkdir(parents=True, exist_ok=True)
    (Path(TEMP_PATH) / f"{fill_name}.png").write_bytes(png)

class PILBuildImage:
    """
    快捷生成图片与操作图片的工具类
    """

    def __init__(
        self,
        w: int,
        h: int,
        paste_image_width: int = 0,
        paste_image_height: int = 0,
        color: Union[str, Tuple[int, int, int], Tuple[int, int, int, int]] = None,
        image_mode: str = "RGBA",
        font_size: int = 10,
        background: Union[Optional[str], BytesIO, Path] = None,
        font: str = "yz.ttf",
        ratio: float = 1,
        is_alpha: bool = False,
        plain_text: Optional[str] = None,
        font_color: Optional[Tuple[int, int, int]] = None,
        image: Image = None,
    ):
        """
        参数：
            :param w: 自定义图片的宽度，w=0时为图片原本宽度
            :param h: 自定义图片的高度，h=0时为图片原本高度
            :param paste_image_width: 当图片做为背景图时，设置贴图的宽度，用于贴图自动换行
            :param paste_image_height: 当图片做为背景图时，设置贴图的高度，用于贴图自动换行
            :param color: 生成图片的颜色
            :param image_mode: 图片的类型
            :param font_size: 文字大小
            :param background: 打开图片的路径
            :param font: 字体，默认在 resource/ttf/ 路径下
            :param ratio: 倍率压缩
            :param is_alpha: 是否背景透明
            :param plain_text: 纯文字文本
        """
        self.w = int(w)
        self.h = int(h)
        self.paste_image_width = int(paste_image_width)
        self.paste_image_height = int(paste_image_height)
        self.current_w = 0
        self.current_h = 0
        self.font = ImageFont.truetype(FONT_PATH + font, int(font_size))
        if not plain_text and not color:
            color = (255, 255, 255)
        if image:
            self.markImg = image
            self.w, self.h = self.markImg.size
            return
        
        if not background:
            if plain_text:
                if not color:
                    color = (255, 255, 255, 0)
                ttf_w, ttf_h = self.getsize(plain_text)
                self.w = self.w if self.w > ttf_w else ttf_w
                self.h = self.h if self.h > ttf_h else ttf_h
            self.markImg = Image.new(image_mode, (self.w, self.h), color)
            self.markImg.convert(image_mode)
        else:
            if not w and not h:
                self.markImg = Image.open(background)
                w, h = self.markImg.size
                if ratio and ratio > 0 and ratio != 1:
                    self.w = int(ratio * w)
                    self.h = int(ratio * h)
                    self.markImg = self.markImg.resize(
                        (self.w, self.h), Image.ANTIALIAS
                    )
                else:
                    self.w = w
                    self.h = h
            else:
                self.markImg = Image.open(background).resize(
                    (self.w, self.h), Image.ANTIALIAS
                )
        if is_alpha:
            array = self.markImg.load()
            for i in range(w):
                for j in range(h):
                    pos = array[i, j]
                    is_edit = sum([1 for x in pos[0:3] if x > 240]) == 3
                    if is_edit:
                        array[i, j] = (255, 255, 255, 0)
        self.draw = ImageDraw.Draw(self.markImg)
        self.size = self.w, self.h
        if plain_text:
            fill = font_color if font_color else (0, 0, 0)
            self.text((0, 0), plain_text, fill)
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            self.loop = asyncio.get_event_loop()

    # 转换为CV2
    def to_cv2(self):
        opencv_image = np.array(self.markImg)
        opencv_image = cv2.cvtColor(opencv_image, cv2.COLOR_RGB2BGR)
        return opencv_image
    
    async def apaste(
        self,
        img: "BuildImage" or Image,
        pos: Tuple[int, int] = None,
        alpha: bool = False,
        center_type: Optional[Literal["center", "by_height", "by_width"]] = None,
    ):
        """
        说明：
            异步 贴图
        参数：
            :param img: 已打开的图片文件，可以为 BuildImage 或 Image
            :param pos: 贴图位置（左上角）
            :param alpha: 图片背景是否为透明
            :param center_type: 居中类型，可能的值 center: 完全居中，by_width: 水平居中，by_height: 垂直居中
        """
        await self.loop.run_in_executor(None, self.paste, img, pos, alpha, center_type)

    def paste(
        self,
        img: "BuildImage" or Image,
        pos: Tuple[int, int] = None,
        alpha: bool = False,
        center_type: Optional[Literal["center", "by_height", "by_width"]] = None,
    ):
        """
        说明：
            贴图
        参数：
            :param img: 已打开的图片文件，可以为 BuildImage 或 Image
            :param pos: 贴图位置（左上角）
            :param alpha: 图片背景是否为透明
            :param center_type: 居中类型，可能的值 center: 完全居中，by_width: 水平居中，by_height: 垂直居中
        """
        if center_type:
            if center_type not in ["center", "by_height", "by_width"]:
                raise ValueError(
                    "center_type must be 'center', 'by_width' or 'by_height'"
                )
            width, height = 0, 0
            if not pos:
                pos = (0, 0)
            if center_type == "center":
                width = int((self.w - img.w) / 2)
                height = int((self.h - img.h) / 2)
            elif center_type == "by_width":
                width = int((self.w - img.w) / 2)
                height = pos[1]
            elif center_type == "by_height":
                width = pos[0]
                height = int((self.h - img.h) / 2)
            pos = (width, height)
        if isinstance(img, PILBuildImage):
            img = img.markImg
        if self.current_w == self.w:
            self.current_w = 0
            self.current_h += self.paste_image_height
        if not pos:
            pos = (self.current_w, self.current_h)
        if alpha:
            try:
                self.markImg.paste(img, pos, img)
            except ValueError:
                img = img.convert("RGBA")
                self.markImg.paste(img, pos, img)
        else:
            self.markImg.paste(img, pos)
        self.current_w += self.paste_image_width

    def getsize(self, msg: str) -> Tuple[int, int]:
        """
        说明：
            获取文字在该图片 font_size 下所需要的空间
        参数：
            :param msg: 文字内容
        """
        return self.font.getsize(msg)

    async def apoint(
        self, pos: Tuple[int, int], fill: Optional[Tuple[int, int, int]] = None
    ):
        """
        说明：
            异步 绘制多个或单独的像素
        参数：
            :param pos: 坐标
            :param fill: 填错颜色
        """
        await self.loop.run_in_executor(None, self.point, pos, fill)

    def point(self, pos: Tuple[int, int], fill: Optional[Tuple[int, int, int]] = None):
        """
        说明：
            绘制多个或单独的像素
        参数：
            :param pos: 坐标
            :param fill: 填错颜色
        """
        self.draw.point(pos, fill=fill)

    async def aellipse(
        self,
        pos: Tuple[int, int, int, int],
        fill: Optional[Tuple[int, int, int]] = None,
        outline: Optional[Tuple[int, int, int]] = None,
        width: int = 1,
    ):
        """
        说明：
            异步 绘制圆
        参数：
            :param pos: 坐标范围
            :param fill: 填充颜色
            :param outline: 描线颜色
            :param width: 描线宽度
        """
        await self.loop.run_in_executor(None, self.ellipse, pos, fill, outline, width)

    def ellipse(
        self,
        pos: Tuple[int, int, int, int],
        fill: Optional[Tuple[int, int, int]] = None,
        outline: Optional[Tuple[int, int, int]] = None,
        width: int = 1,
    ):
        """
        说明：
            绘制圆
        参数：
            :param pos: 坐标范围
            :param fill: 填充颜色
            :param outline: 描线颜色
            :param width: 描线宽度
        """
        self.draw.ellipse(pos, fill, outline, width)

    async def atext(
        self,
        pos: Tuple[int, int],
        text: str,
        fill: Tuple[int, int, int] = (0, 0, 0),
        center_type: Optional[Literal["center", "by_height", "by_width"]] = None,
    ):
        """
        说明：
            异步 在图片上添加文字
        参数：
            :param pos: 文字位置
            :param text: 文字内容
            :param fill: 文字颜色
            :param center_type: 居中类型，可能的值 center: 完全居中，by_width: 水平居中，by_height: 垂直居中
        """
        await self.loop.run_in_executor(None, self.text, pos, text, fill, center_type)

    def text(
        self,
        pos: Tuple[int, int],
        text: str,
        fill: Tuple[int, int, int] = (0, 0, 0),
        center_type: Optional[Literal["center", "by_height", "by_width"]] = None,
    ):
        """
        说明：
            在图片上添加文字
        参数：
            :param pos: 文字位置
            :param text: 文字内容
            :param fill: 文字颜色
            :param center_type: 居中类型，可能的值 center: 完全居中，by_width: 水平居中，by_height: 垂直居中
        """
        if center_type:
            if center_type not in ["center", "by_height", "by_width"]:
                raise ValueError(
                    "center_type must be 'center', 'by_width' or 'by_height'"
                )
            w, h = self.w, self.h
            ttf_w, ttf_h = self.getsize(text)
            if center_type == "center":
                w = int((w - ttf_w) / 2)
                h = int((h - ttf_h) / 2)
            elif center_type == "by_width":
                w = int((w - ttf_w) / 2)
                h = pos[1]
            elif center_type == "by_height":
                h = int((h - ttf_h) / 2)
                w = pos[0]
            pos = (w, h)
        self.draw.text(pos, text, fill=fill, font=self.font)

    async def asave(self, path: Union[str, Path]):
        """
        说明：
            异步 保存图片
        参数：
            :param path: 图片路径
        """
        await self.loop.run_in_executor(None, self.save, path)

    def save(self, path: Union[str, Path]):
        """
        说明：
            保存图片
        参数：
            :param path: 图片路径
        """
        if isinstance(path, Path):
            path = path.absolute()
        self.markImg.save(path)

    def show(self):
        """
        说明：
            显示图片
        """
        self.markImg.show(self.markImg)

    async def aresize(self, ratio: float = 0, w: int = 0, h: int = 0):
        """
        说明：
            异步 压缩图片
        参数：
            :param ratio: 压缩倍率
            :param w: 压缩图片宽度至 w
            :param h: 压缩图片高度至 h
        """
        await self.loop.run_in_executor(None, self.resize, ratio, w, h)

    def resize(self, ratio: float = 0, w: int = 0, h: int = 0):
        """
        说明：
            压缩图片
        参数：
            :param ratio: 压缩倍率
            :param w: 压缩图片宽度至 w
            :param h: 压缩图片高度至 h
        """
        if not w and not h and not ratio:
            raise Exception("缺少参数...")
        if not w and not h and ratio:
            w = int(self.w * ratio)
            h = int(self.h * ratio)
        self.markImg = self.markImg.resize((w, h), Image.ANTIALIAS)
        self.w, self.h = self.markImg.size
        self.size = self.w, self.h
        self.draw = ImageDraw.Draw(self.markImg)

    async def acrop(self, box: Tuple[int, int, int, int]):
        """
        说明：
            异步 裁剪图片
        参数：
            :param box: 左上角坐标，右下角坐标 (left, upper, right, lower)
        """
        await self.loop.run_in_executor(None, self.crop, box)

    def crop(self, box: Tuple[int, int, int, int]):
        """
        说明：
            裁剪图片
        参数：
            :param box: 左上角坐标，右下角坐标 (left, upper, right, lower)
        """
        self.markImg = self.markImg.crop(box)
        self.w, self.h = self.markImg.size
        self.size = self.w, self.h
        self.draw = ImageDraw.Draw(self.markImg)

    def check_font_size(self, word: str) -> bool:
        """
        说明：
            检查文本所需宽度是否大于图片宽度
        参数：
            :param word: 文本内容
        """
        return self.font.getsize(word)[0] > self.w

    async def atransparent(self, alpha_ratio: float = 1, n: int = 0):
        """
        说明：
            异步 图片透明化
        参数：
            :param alpha_ratio: 透明化程度
            :param n: 透明化大小内边距
        """
        await self.loop.run_in_executor(None, self.transparent, alpha_ratio, n)

    def transparent(self, alpha_ratio: float = 1, n: int = 0):
        """
        说明：
            图片透明化
        参数：
            :param alpha_ratio: 透明化程度
            :param n: 透明化大小内边距
        """
        self.markImg = self.markImg.convert("RGBA")
        x, y = self.markImg.size
        for i in range(n, x - n):
            for k in range(n, y - n):
                color = self.markImg.getpixel((i, k))
                color = color[:-1] + (int(100 * alpha_ratio),)
                self.markImg.putpixel((i, k), color)
        self.draw = ImageDraw.Draw(self.markImg)

    def pic2bs4(self) -> str:
        """
        说明：
            BuildImage 转 base64
        """
        buf = BytesIO()
        self.markImg.save(buf, format="PNG")
        base64_str = base64.b64encode(buf.getvalue()).decode()
        return base64_str

    def convert(self, type_: str):
        """
        说明：
            修改图片类型
        参数：
            :param type_: 类型
        """
        self.markImg = self.markImg.convert(type_)

    async def arectangle(
        self,
        xy: Tuple[int, int, int, int],
        fill: Optional[Tuple[int, int, int]] = None,
        outline: str = None,
        width: int = 1,
    ):
        """
        说明：
            异步 画框
        参数：
            :param xy: 坐标
            :param fill: 填充颜色
            :param outline: 轮廓颜色
            :param width: 线宽
        """
        await self.loop.run_in_executor(None, self.rectangle, xy, fill, outline, width)

    def rectangle(
        self,
        xy: Tuple[int, int, int, int],
        fill: Optional[Tuple[int, int, int]] = None,
        outline: str = None,
        width: int = 1,
    ):
        """
        说明：
            画框
        参数：
            :param xy: 坐标
            :param fill: 填充颜色
            :param outline: 轮廓颜色
            :param width: 线宽
        """
        self.draw.rectangle(xy, fill, outline, width)

    async def apolygon(
        self,
        xy: List[Tuple[int, int]],
        fill: Tuple[int, int, int] = (0, 0, 0),
        outline: int = 1,
    ):
        """
        说明:
            异步 画多边形
        参数：
            :param xy: 坐标
            :param fill: 颜色
            :param outline: 线宽
        """
        await self.loop.run_in_executor(None, self.polygon, xy, fill, outline)

    def polygon(
        self,
        xy: List[Tuple[int, int]],
        fill: Tuple[int, int, int] = (0, 0, 0),
        outline: int = 1,
    ):
        """
        说明:
            画多边形
        参数：
            :param xy: 坐标
            :param fill: 颜色
            :param outline: 线宽
        """
        self.draw.polygon(xy, fill, outline)

    async def aline(
        self,
        xy: Tuple[int, int, int, int],
        fill: Optional[Tuple[int, int, int]] = None,
        width: int = 1,
    ):
        """
        说明：
            异步 画线
        参数：
            :param xy: 坐标
            :param fill: 填充
            :param width: 线宽
        """
        await self.loop.run_in_executor(None, self.line, xy, fill, width)

    def line(
        self,
        xy: Tuple[int, int, int, int],
        fill: Optional[Tuple[int, int, int]] = None,
        width: int = 1,
    ):
        """
        说明：
            画线
        参数：
            :param xy: 坐标
            :param fill: 填充
            :param width: 线宽
        """
        self.draw.line(xy, fill, width)

    async def acircle(self):
        """
        说明：
            异步 将 BuildImage 图片变为圆形
        """
        await self.loop.run_in_executor(None, self.circle)

    def circle(self):
        """
        说明：
            将 BuildImage 图片变为圆形
        """
        self.convert("RGBA")
        r2 = min(self.w, self.h)
        if self.w != self.h:
            self.resize(w=r2, h=r2)
        r3 = int(r2 / 2)
        imb = Image.new("RGBA", (r3 * 2, r3 * 2), (255, 255, 255, 0))
        pim_a = self.markImg.load()  # 像素的访问对象
        pim_b = imb.load()
        r = float(r2 / 2)
        for i in range(r2):
            for j in range(r2):
                lx = abs(i - r)  # 到圆心距离的横坐标
                ly = abs(j - r)  # 到圆心距离的纵坐标
                l = (pow(lx, 2) + pow(ly, 2)) ** 0.5  # 三角函数 半径
                if l < r3:
                    pim_b[i - (r - r3), j - (r - r3)] = pim_a[i, j]
        self.markImg = imb

    async def acircle_corner(self, radii: int = 30):
        """
        说明：
            异步 矩形四角变圆
        参数：
            :param radii: 半径
        """
        await self.loop.run_in_executor(None, self.circle_corner, radii)

    def circle_corner(self, radii: int = 30):
        """
        说明：
            矩形四角变圆
        参数：
            :param radii: 半径
        """
        # 画圆（用于分离4个角）
        circle = Image.new("L", (radii * 2, radii * 2), 0)
        draw = ImageDraw.Draw(circle)
        draw.ellipse((0, 0, radii * 2, radii * 2), fill=255)
        self.markImg = self.markImg.convert("RGBA")
        w, h = self.markImg.size
        alpha = Image.new("L", self.markImg.size, 255)
        alpha.paste(circle.crop((0, 0, radii, radii)), (0, 0))
        alpha.paste(circle.crop((radii, 0, radii * 2, radii)), (w - radii, 0))
        alpha.paste(
            circle.crop((radii, radii, radii * 2, radii * 2)), (w - radii, h - radii)
        )
        alpha.paste(circle.crop((0, radii, radii, radii * 2)), (0, h - radii))
        self.markImg.putalpha(alpha)

    async def arotate(self, angle: int, expand: bool = False):
        """
        说明：
            异步 旋转图片
        参数：
            :param angle: 角度
            :param expand: 放大图片适应角度
        """
        await self.loop.run_in_executor(None, self.rotate, angle, expand)

    def rotate(self, angle: int, expand: bool = False):
        """
        说明：
            旋转图片
        参数：
            :param angle: 角度
            :param expand: 放大图片适应角度
        """
        self.markImg = self.markImg.rotate(angle, expand=expand)

    async def atranspose(self, angle: int):
        """
        说明：
            异步 旋转图片(包括边框)
        参数：
            :param angle: 角度
        """
        await self.loop.run_in_executor(None, self.transpose, angle)

    def transpose(self, angle: int):
        """
        说明：
            旋转图片(包括边框)
        参数：
            :param angle: 角度
        """
        self.markImg.transpose(angle)

    async def afilter(self, filter_: str, aud: int = None):
        """
        说明：
            异步 图片变化
        参数：
            :param filter_: 变化效果
            :param aud: 利率
        """
        await self.loop.run_in_executor(None, self.filter, filter_, aud)

    def filter(self, filter_: str, aud: int = None):
        """
        说明：
            图片变化
        参数：
            :param filter_: 变化效果
            :param aud: 利率
        """
        _x = None
        if filter_ == "GaussianBlur":  # 高斯模糊
            _x = ImageFilter.GaussianBlur
        elif filter_ == "EDGE_ENHANCE":  # 锐化效果
            _x = ImageFilter.EDGE_ENHANCE
        elif filter_ == "BLUR":  # 模糊效果
            _x = ImageFilter.BLUR
        elif filter_ == "CONTOUR":  # 铅笔滤镜
            _x = ImageFilter.CONTOUR
        elif filter_ == "FIND_EDGES":  # 边缘检测
            _x = ImageFilter.FIND_EDGES
        if _x:
            if aud:
                self.markImg = self.markImg.filter(_x(aud))
            else:
                self.markImg = self.markImg.filter(_x)
        self.draw = ImageDraw.Draw(self.markImg)

    #
    def getchannel(self, type_):
        self.markImg = self.markImg.getchannel(type_)

class Cv2BuildImage:
    image_path: str = None
    image: None
    w: int
    h: int

    def __init__(self, 
        image_path: str = None,
        image: PILBuildImage = None,
        background_color: str = None,
        h: int = None,
        w: int = None,
        is_alpha: bool = False,
        ):
        """
        初始化
        :param image_path: 图片路径
        """
        if image_path:
            self.image_path = image_path
            self.image = cv2.imread(image_path)
            # 检测背景透明
            if is_alpha:
                self.image = cv2.cvtColor(self.image, cv2.COLOR_BGR2RGBA)
        elif image:
            self.image = image.to_cv2()
        elif h and w:
            if is_alpha:
                self.image = np.zeros((h, w, 4), np.uint8)
            else:
                self.image = np.zeros((h, w, 3), np.uint8)
            if background_color:
                # 16进制颜色转换为RGB
                background_color = background_color.lstrip("#")
                print(background_color)
                background_color = tuple(int(background_color[i:i + 2], 16) for i in (0, 2, 4))
        else:
            raise ValueError("参数错误...")
        self.w, self.h, _ = self.image.shape

    def to_PilBuildImage(self) -> PILBuildImage:
        """
        转换为PILBuildImage
        """
        image = Image.fromarray(cv2.cvtColor(self.image, cv2.COLOR_BGR2RGB))
        return PILBuildImage(w=self.w, h=self.h, image=image)
    
    def crop(self, x: int, y: int, width: int, height: int):
        """
        裁剪图片
        :param x: x坐标
        :param y: y坐标
        :param width: 宽度
        :param height: 高度
        """
        self.image = self.image[y:y + height, x:x + width]

    def paste(self, image_path: str | PILBuildImage, x: int, y: int):
        """
        粘贴图片
        :param image_path: 图片路径
        :param x: x坐标
        :param y: y坐标
        """
        if isinstance(image_path, PILBuildImage):
            image = image_path.to_cv2()
        # 检测是不是字符串
        elif isinstance(image_path, str):
            image = cv2.imread(image_path)
        elif isinstance(image_path, Cv2BuildImage):
            image = image_path.image
        elif isinstance(image_path, np.ndarray):
            image = image_path
        else:
            print(type(image_path))
            raise ValueError("image_path应为str或PILBuildImage类型")

        self.image[y:y + image.shape[0], x:x + image.shape[1]] = image

    def resize(self, width, height):
        """
        重置图片大小
        :param width: 宽度
        :param height: 高度
        """
        self.image = cv2.resize(self.image, (width, height))

    def shape(self) -> Tuple[int, int, int]:
        """
        获取图片尺寸
        """
        return self.image.shape

    def circle_corner (self, radii: int = 30):
        """
        说明：
            矩形四角变圆
        参数：
            :param radii: 半径
        """
        # 获取图像的宽度和高度
        h, w = self.image.shape[:2]

        # 创建一个和原始图像大小相同的掩码
        mask = np.zeros((h, w), np.uint8)

        # 在掩码上画一个填充的圆角矩形
        cv2.rectangle(mask, (radii, radii), (w - radii, h - radii), 255, -1)
        cv2.circle(mask, (radii, radii), radii, 255, -1)
        cv2.circle(mask, (w - radii, radii), radii, 255, -1)
        cv2.circle(mask, (radii, h - radii), radii, 255, -1)
        cv2.circle(mask, (w - radii, h - radii), radii, 255, -1)

        # 使用掩码和原始图像进行位运算
        self.image = cv2.bitwise_and(self.image, self.image, mask=mask)

    def save(self, output_path):
        """
        保存图片
        :param output_path: 保存路径
        """
        cv2.imwrite(output_path, self.image)

    def show(self):
        cv2.imshow("Image", self.image)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

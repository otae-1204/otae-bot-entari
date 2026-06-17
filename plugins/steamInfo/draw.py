import time
import aiohttp
import calendar
import numpy as np
from io import BytesIO
from pathlib import Path
from typing import List, Dict, Tuple
from colorsys import rgb_to_hsv, hsv_to_rgb
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

from configs.path_config import IMAGE_PATH, FONT_PATH
from .models import Player


WIDTH = 400
PARENT_AVATAR_SIZE = 72
MEMBER_AVATAR_SIZE = 50

steam_image_path = Path(IMAGE_PATH) / "steamInfo"
font_regular_path = FONT_PATH + "steamInfo/MiSans-Regular.ttf"
font_light_path = FONT_PATH + "steamInfo/MiSans-Light.ttf"
font_bold_path = FONT_PATH + "steamInfo/MiSans-Bold.ttf"

unknown_avatar_path = steam_image_path / "unknown_avatar.jpg"
parent_status_path = steam_image_path / "parent_status.png"
friends_search_path = steam_image_path / "friends_search.png"
busy_path = steam_image_path / "busy.png"
zzz_online_path = steam_image_path / "zzz_online.png"
zzz_gaming_path = steam_image_path / "zzz_gaming.png"
gaming_path = steam_image_path / "gaming.png"


def check_font():
    if not Path(font_regular_path).exists():
        raise FileNotFoundError(f"Font file {font_regular_path} not found.")
    if not Path(font_light_path).exists():
        raise FileNotFoundError(f"Font file {font_light_path} not found.")
    if not Path(font_bold_path).exists():
        raise FileNotFoundError(f"Font file {font_bold_path} not found.")


def _unknown_avatar() -> Image.Image:
    with Image.open(unknown_avatar_path) as image:
        return image.copy()


def _load_image_copy(path: Path) -> Image.Image:
    with Image.open(path) as image:
        image.load()
        return image.copy()


async def _fetch_avatar(avatar_url: str, proxy: str = None) -> Tuple[Image.Image, bool]:
    for request_proxy in ([proxy] if proxy else [None]):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                async with session.get(avatar_url, proxy=request_proxy) as resp:
                    if resp.status != 200:
                        continue
                    image = Image.open(BytesIO(await resp.read()))
                    image.load()
                    return image.copy(), True
        except Exception:
            continue
    return _unknown_avatar(), False


async def fetch_avatar(player: Player, avatar_dir: Path, proxy: str = None) -> Image.Image:
    if avatar_dir is not None:
        avatar_dir.mkdir(parents=True, exist_ok=True)
        avatar_path = avatar_dir / f"avatar_{player['steamid']}.png"

        if avatar_path.exists():
            try:
                return _load_image_copy(avatar_path)
            except Exception:
                avatar_path.unlink(missing_ok=True)

        avatar, fetched = await _fetch_avatar(player["avatarfull"], proxy)
        if fetched:
            avatar.save(avatar_path)
        else:
            return avatar
    else:
        avatar, _ = await _fetch_avatar(player["avatarfull"], proxy)
    
    return avatar


async def simplize_steam_player_data(
    player: Player, proxy: str = None, avatar_dir: Path = None
) -> Dict[str, str]:
    avatar = await fetch_avatar(player, avatar_dir, proxy)

    if player["personastate"] == 0:
        if not player.get("lastlogoff"):
            status = "离线"
        else:
            time_logged_off = player["lastlogoff"]  # Unix timestamp
            time_to_now = calendar.timegm(time.gmtime()) - time_logged_off

            # 将时间转换为自然语言
            if time_to_now < 60:
                status = "上次在线 刚刚"
            elif time_to_now < 3600:
                status = f"上次在线 {time_to_now // 60} 分钟前"
            elif time_to_now < 86400:
                status = f"上次在线 {time_to_now // 3600} 小时前"
            elif time_to_now < 2592000:
                status = f"上次在线 {time_to_now // 86400} 天前"
            elif time_to_now < 31536000:
                status = f"上次在线 {time_to_now // 2592000} 个月前"
            else:
                status = f"上次在线 {time_to_now // 31536000} 年前"
    elif player["personastate"] in [1, 2, 4]:
        status = (
            "在线" if player.get("gameextrainfo") is None else player["gameextrainfo"]
        )
    elif player["personastate"] == 3:
        status = (
            "离开" if player.get("gameextrainfo") is None else player["gameextrainfo"]
        )
    elif player["personastate"] in [5, 6]:
        status = "在线"
    else:
        status = "未知"

    return {
        "avatar": avatar,
        "name": player["personaname"],
        "status": status,
        "personastate": player["personastate"],
    }


def image_to_bytes(image: Image.Image) -> bytes:
    with BytesIO() as bio:
        image.save(bio, format="PNG")
        return bio.getvalue()


def hex_to_rgb(hex_color: str):
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))


def _fit_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    ellipsis: str = "...",
) -> str:
    if font.getlength(text) <= max_width:
        return text

    ellipsis_width = font.getlength(ellipsis)
    result = ""
    for char in text:
        if font.getlength(result + char) + ellipsis_width > max_width:
            break
        result += char
    return result + ellipsis


def _square_cover(image: Image.Image, size: int) -> Image.Image:
    image = image.convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side)).resize(
        (size, size), Image.BICUBIC
    )


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill,
):
    left, top, right, bottom = box
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    draw.text(
        (
            left + (right - left - text_width) / 2,
            top + (bottom - top - text_height) / 2 - bbox[1],
        ),
        text,
        font=font,
        fill=fill,
    )


personastate_colors = {
    0: (hex_to_rgb("969697"), hex_to_rgb("656565")),
    1: (hex_to_rgb("6dcef5"), hex_to_rgb("4c91ac")),
    2: (hex_to_rgb("6dcef5"), hex_to_rgb("4c91ac")),
    3: (hex_to_rgb("45778e"), hex_to_rgb("365969")),
    4: (hex_to_rgb("6dcef5"), hex_to_rgb("4c91ac")),
    5: (hex_to_rgb("6dcef5"), hex_to_rgb("4c91ac")),
    6: (hex_to_rgb("6dcef5"), hex_to_rgb("4c91ac")),
}


def vertically_concatenate_images(images: List[Image.Image]) -> Image.Image:
    widths, heights = zip(*(i.size for i in images))
    total_width = max(widths)
    total_height = sum(heights)

    new_image = Image.new("RGB", (total_width, total_height))

    y_offset = 0
    for image in images:
        new_image.paste(image, (0, y_offset))
        y_offset += image.size[1]

    return new_image


def draw_start_gaming(avatar: Image.Image, friend_name: str, game_name: str):
    canvas = Image.open(gaming_path)
    canvas.paste(avatar.resize((66, 66), Image.BICUBIC), (15, 20))

    # 绘制名称
    draw = ImageDraw.Draw(canvas)
    draw.text((104, 14), friend_name, font=ImageFont.truetype(font_regular_path, 19), fill=hex_to_rgb("e3ffc2"))

    # 绘制"正在玩"
    draw.text((103, 42), "正在玩", font=ImageFont.truetype(font_regular_path, 17), fill=hex_to_rgb("969696"))

    # 绘制游戏名称
    draw.text((104, 66), game_name, font=ImageFont.truetype(font_bold_path, 14), fill=hex_to_rgb("91c257"))

    return canvas


def draw_parent_status(parent_avatar: Image.Image, parent_name: str) -> Image.Image:
    parent_avatar = parent_avatar.resize(
        (PARENT_AVATAR_SIZE, PARENT_AVATAR_SIZE), Image.BICUBIC
    )

    canvas = Image.open(parent_status_path).resize((WIDTH, 120), Image.BICUBIC)

    draw = ImageDraw.Draw(canvas)

    # 在左下角 (16, 16) 处绘制头像
    avatar_height = 120 - 16 - PARENT_AVATAR_SIZE
    canvas.paste(parent_avatar, (16, avatar_height))

    # 绘制名称
    draw.text(
        (16 + PARENT_AVATAR_SIZE + 16, avatar_height + 12),
        parent_name,
        font=ImageFont.truetype(font_bold_path, 20),
        fill=hex_to_rgb("6dcff6"),
    )

    # 绘制状态
    draw.text(
        (16 + PARENT_AVATAR_SIZE + 16, avatar_height + 20 + 16),
        "在线",
        font=ImageFont.truetype(font_light_path, 18),
        fill=hex_to_rgb("4c91ac"),
    )

    return canvas


def draw_friends_search() -> Image.Image:
    canvas = Image.new("RGB", (WIDTH, 50), hex_to_rgb("434953"))

    friends_search = Image.open(friends_search_path)

    canvas.paste(friends_search, (WIDTH - friends_search.width, 0))

    draw = ImageDraw.Draw(canvas)

    draw.text(
        (24, 10),
        "好友",
        hex_to_rgb("b7ccd5"),
        font=ImageFont.truetype(font_regular_path, 20),
    )

    return canvas


def draw_friend_status(
    friend_avatar: Image.Image, friend_name: str, status: str, personastate: int
) -> Image.Image:
    friend_avatar = friend_avatar.resize(
        (MEMBER_AVATAR_SIZE, MEMBER_AVATAR_SIZE), Image.BICUBIC
    )

    canvas = Image.new("RGB", (WIDTH, 64), hex_to_rgb("1e2024"))

    draw = ImageDraw.Draw(canvas)

    if personastate == 2:
        # 忙碌 加上一个忙碌图标
        canvas = draw_friend_status(friend_avatar, friend_name, status, 1)
        draw = ImageDraw.Draw(canvas)

        busy = Image.open(busy_path)

        name_width = int(
            draw.textlength(friend_name, font=ImageFont.truetype(font_bold_path, 20))
        )

        canvas.paste(busy, (22 + MEMBER_AVATAR_SIZE + 16 + name_width + 4, 18))

        return canvas

    if personastate == 4:
        # 打盹 加上一个 ZZZ
        canvas = draw_friend_status(friend_avatar, friend_name, status, 1)
        draw = ImageDraw.Draw(canvas)

        zzz = Image.open(zzz_online_path if status == "在线" else zzz_gaming_path)

        name_width = int(
            draw.textlength(friend_name, font=ImageFont.truetype(font_bold_path, 20))
        )

        canvas.paste(zzz, (22 + MEMBER_AVATAR_SIZE + 16 + name_width + 8, 18))

        return canvas

    # 绘制头像
    canvas.paste(friend_avatar, (22, 8))

    if status != "在线" and personastate == 1:
        fill = (hex_to_rgb("e3ffc2"), hex_to_rgb("8ebe56"))
    elif status != "离开" and personastate == 3:
        fill = (hex_to_rgb("e3ffc2"), hex_to_rgb("8ebe56"))
    else:
        fill = personastate_colors[personastate]

    # 绘制名称
    draw.text(
        (22 + MEMBER_AVATAR_SIZE + 18, 12),
        friend_name,
        font=ImageFont.truetype(font_bold_path, 20),
        fill=fill[0],
    )

    # 绘制状态
    draw.text(
        (22 + MEMBER_AVATAR_SIZE + 16, 36),
        status,
        font=ImageFont.truetype(font_regular_path, 18),
        fill=fill[1],
    )

    return canvas


def draw_gaming_friends_status(data: List[Dict[str, str]]) -> Image.Image:
    canvas = Image.new(
        "RGB",
        (WIDTH, 64 + (MEMBER_AVATAR_SIZE + 16) * len(data) + 16),
        hex_to_rgb("1e2024"),
    )

    draw = ImageDraw.Draw(canvas)

    # 绘制标题
    draw.text(
        (22, 22),
        "游戏中",
        hex_to_rgb("c5d6d4"),
        font=ImageFont.truetype(font_regular_path, 22),
    )

    # 绘制好友头像和名称
    friends_status_list = [
        draw_friend_status(d["avatar"], d["name"], d["status"], d["personastate"])
        for d in data
    ]

    # 拼接好友头像和名称
    for i, friend_status in enumerate(friends_status_list):
        canvas.paste(friend_status, (0, 64 + (MEMBER_AVATAR_SIZE + 16) * i))

    return canvas


def draw_online_friends_status(data: List[Dict[str, str]]) -> Image.Image:
    canvas = Image.new(
        "RGB",
        (WIDTH, 64 + (MEMBER_AVATAR_SIZE + 16) * len(data) + 16),
        hex_to_rgb("1e2024"),
    )

    draw = ImageDraw.Draw(canvas)

    # 绘制标题
    draw.text(
        (22, 22),
        "在线好友",
        hex_to_rgb("c5d6d4"),
        font=ImageFont.truetype(font_regular_path, 22),
    )

    # 绘制在线人数
    draw.text(
        (115, 25),
        f"({len(data)})",
        hex_to_rgb("67665c"),
        font=ImageFont.truetype(font_regular_path, 18),
    )

    # 绘制好友头像和名称
    friends_status_list = [
        draw_friend_status(d["avatar"], d["name"], d["status"], d["personastate"])
        for d in data
    ]

    # 拼接好友头像和名称
    for i, friend_status in enumerate(friends_status_list):
        canvas.paste(friend_status, (0, 64 + (MEMBER_AVATAR_SIZE + 16) * i))

    return canvas


def draw_offline_friends_status(data: List[Dict[str, str]]) -> Image.Image:
    canvas = Image.new(
        "RGB",
        (WIDTH, 64 + (MEMBER_AVATAR_SIZE + 16) * len(data) + 16),
        hex_to_rgb("1e2024"),
    )

    draw = ImageDraw.Draw(canvas)

    # 绘制标题
    draw.text(
        (22, 22),
        "离线",
        hex_to_rgb("c5d6d4"),
        font=ImageFont.truetype(font_regular_path, 22),
    )

    # 绘制离线人数
    draw.text(
        (72, 25),
        f"({len(data)})",
        hex_to_rgb("67665c"),
        font=ImageFont.truetype(font_regular_path, 18),
    )

    # 绘制好友头像和名称
    friends_status_list = [
        draw_friend_status(d["avatar"], d["name"], d["status"], d["personastate"])
        for d in data
    ]

    # 拼接好友头像和名称
    for i, friend_status in enumerate(friends_status_list):
        canvas.paste(friend_status, (0, 64 + (MEMBER_AVATAR_SIZE + 16) * i))

    return canvas


def draw_friends_status(
    parent_avatar: Image.Image, parent_name: str, data: List[Dict[str, str]]
):
    data.sort(key=lambda x: x["personastate"])

    parent_status = draw_parent_status(parent_avatar, parent_name)
    friends_search = draw_friends_search()

    status_images: List[Image.Image] = []
    height = parent_status.height + friends_search.height

    gaming_data = [
        d
        for d in data
        if (d["personastate"] == 1 and d["status"] != "在线")
        or (d["personastate"] == 3 and d["status"] != "离开")
        or (d["personastate"] == 4 and d["status"] != "在线")
    ]

    if gaming_data:
        status_images.append(draw_gaming_friends_status(gaming_data))
        height += status_images[-1].height

    online_data = [
        d
        for d in data
        if (d["personastate"] == 1 and d["status"] == "在线")
        or (d["personastate"] == 3 and d["status"] == "离开")
        or (d["personastate"] == 4 and d["status"] == "在线")
        or (d["personastate"] in [2, 5, 6])
    ]
    # 按 1, 2, 4, 5, 6, 3 的顺序排序
    online_data.sort(key=lambda x: (7 if x["personastate"] == 3 else x["personastate"]))

    if online_data:
        status_images.append(draw_online_friends_status(online_data))
        height += status_images[-1].height

    offline_data = [d for d in data if d["personastate"] == 0]
    if offline_data:
        status_images.append(draw_offline_friends_status(offline_data))
        height += status_images[-1].height

    # 拼合图片
    canvas = Image.new("RGB", (WIDTH, height), hex_to_rgb("1e2024"))
    draw = ImageDraw.Draw(canvas)

    canvas.paste(parent_status, (0, 0))
    canvas.paste(friends_search, (0, parent_status.height))

    y = parent_status.height + friends_search.height

    for i, status_image in enumerate(status_images):
        canvas.paste(status_image, (0, y))
        y += status_image.height

        # 绘制分割线
        if i != len(status_images) - 1:
            draw.rectangle([0, y - 1, WIDTH, y], fill=hex_to_rgb("333439"))

    return canvas


def draw_game_stats(
    parent_avatar: Image.Image,
    parent_name: str,
    app_icon: Image.Image,
    app_name: str,
    app_id: int,
    total_players: int,
    bound_players: int,
    total_time_text: str,
    recent_time_text: str,
    rows: List[Dict[str, str]],
) -> Image.Image:
    row_height = 92
    summary_height = 142
    ranking_header_height = 48
    bottom_padding = 14
    height = (
        120
        + summary_height
        + ranking_header_height
        + row_height * len(rows)
        + bottom_padding
    )

    canvas = Image.new("RGB", (WIDTH, height), hex_to_rgb("1e2024"))
    draw = ImageDraw.Draw(canvas)

    header = Image.open(parent_status_path).resize((WIDTH, 120), Image.BICUBIC)
    parent_avatar = parent_avatar.resize(
        (PARENT_AVATAR_SIZE, PARENT_AVATAR_SIZE), Image.BICUBIC
    )
    header.paste(parent_avatar, (16, 32))
    header_draw = ImageDraw.Draw(header)
    parent_font = ImageFont.truetype(font_bold_path, 20)
    header_draw.text(
        (104, 34),
        _fit_text(parent_name, parent_font, 260),
        font=parent_font,
        fill=hex_to_rgb("6dcff6"),
    )
    header_draw.text(
        (104, 64),
        "Steam 游戏统计",
        font=ImageFont.truetype(font_light_path, 18),
        fill=hex_to_rgb("4c91ac"),
    )
    canvas.paste(header, (0, 0))

    y = 120
    draw.rectangle((0, y, WIDTH, y + summary_height), fill=hex_to_rgb("252a31"))
    app_icon = _square_cover(app_icon, 64)
    canvas.paste(app_icon, (22, y + 18))
    draw.rectangle((22, y + 18, 86, y + 82), outline=hex_to_rgb("434953"), width=1)

    title_font = ImageFont.truetype(font_bold_path, 20)
    appid_font = ImageFont.truetype(font_regular_path, 14)
    draw.text(
        (104, y + 19),
        _fit_text(app_name, title_font, 260),
        font=title_font,
        fill=hex_to_rgb("e3ffc2"),
    )
    draw.text(
        (104, y + 49),
        f"AppID {app_id}",
        font=appid_font,
        fill=hex_to_rgb("969696"),
    )
    metric_font = ImageFont.truetype(font_bold_path, 14)
    count_font = metric_font
    small_metric_font = ImageFont.truetype(font_regular_path, 13)
    metric_y = y + 88
    metric_boxes = [
        (22, 106, "公开数据", f"{total_players}/{bound_players}", hex_to_rgb("91c257")),
        (140, 116, "累计", total_time_text, hex_to_rgb("b7ccd5")),
        (268, 110, "近两周", recent_time_text, hex_to_rgb("b7ccd5")),
    ]
    for metric_x, metric_width, label, value, value_color in metric_boxes:
        draw.rounded_rectangle(
            (
                metric_x,
                metric_y,
                metric_x + metric_width,
                metric_y + 42,
            ),
            radius=4,
            fill=hex_to_rgb("20242a"),
            outline=hex_to_rgb("333a42"),
            width=1,
        )
        draw.text(
            (metric_x + 10, metric_y + 5),
            label,
            font=small_metric_font,
            fill=hex_to_rgb("676f75"),
        )
        value_font = count_font if label == "公开数据" else metric_font
        draw.text(
            (metric_x + 10, metric_y + 21),
            _fit_text(value, value_font, metric_width - 18),
            font=value_font,
            fill=value_color,
        )

    y += summary_height
    draw.rectangle((0, y, WIDTH, y + ranking_header_height), fill=hex_to_rgb("434953"))
    draw.text(
        (22, y + 11),
        "群内排行",
        font=ImageFont.truetype(font_regular_path, 20),
        fill=hex_to_rgb("b7ccd5"),
    )
    draw.text(
        (WIDTH - 112, y + 15),
        "总时长 / 近两周",
        font=ImageFont.truetype(font_regular_path, 14),
        fill=hex_to_rgb("969696"),
    )

    y += ranking_header_height
    name_font = ImageFont.truetype(font_bold_path, 18)
    info_font = ImageFont.truetype(font_regular_path, 14)
    time_font = ImageFont.truetype(font_bold_path, 15)
    small_font = ImageFont.truetype(font_regular_path, 13)
    achievement_font = ImageFont.truetype(font_regular_path, 13)
    rank_font = ImageFont.truetype(font_bold_path, 16)

    for index, row in enumerate(rows, start=1):
        row_bg = hex_to_rgb("202329") if index % 2 else hex_to_rgb("1b1d21")
        draw.rectangle((0, y, WIDTH, y + row_height), fill=row_bg)
        _draw_centered_text(
            draw,
            (10, y, 30, y + row_height),
            str(index),
            rank_font,
            fill=hex_to_rgb("6dcef5") if index <= 3 else hex_to_rgb("67665c"),
        )

        avatar = row.get("avatar") or Image.open(unknown_avatar_path)
        avatar = avatar.resize((46, 46), Image.BICUBIC)
        canvas.paste(avatar, (42, y + (row_height - 46) // 2))

        name = _fit_text(str(row["name"]), name_font, 150)
        draw.text((100, y + 11), name, font=name_font, fill=hex_to_rgb("cdefff"))
        draw.text(
            (100, y + 37),
            f"最后游玩 {row['last_text']}",
            font=info_font,
            fill=hex_to_rgb("969696"),
        )

        total_text = row["total_text"]
        recent_text = row["recent_text"]
        draw.text(
            (WIDTH - 22 - time_font.getlength(total_text), y + 13),
            total_text,
            font=time_font,
            fill=hex_to_rgb("6dcff6"),
        )
        draw.text(
            (WIDTH - 22 - small_font.getlength(recent_text), y + 37),
            recent_text,
            font=small_font,
            fill=(
                hex_to_rgb("4c91ac")
                if int(row.get("recent", 0))
                else hex_to_rgb("656565")
            ),
        )

        achievement_completed = row.get("achievement_completed")
        achievement_total = row.get("achievement_total")
        if achievement_completed is not None and achievement_total:
            achievement_text = f"成就 {achievement_completed}/{achievement_total}"
            achievement_color = hex_to_rgb("6dcff6")
        else:
            achievement_text = "成就 无公开数据"
            achievement_color = hex_to_rgb("656565")
        draw.text(
            (100, y + 60),
            achievement_text,
            font=achievement_font,
            fill=achievement_color,
        )
        y += row_height

    return canvas



def _open_image(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGBA")


def get_average_color(image: Image.Image) -> tuple[int, int, int]:
    image_np = np.array(image.convert("RGB"))
    average_color = image_np.mean(axis=(0, 1)).astype(int)
    return tuple(average_color)


def split_image(
    image: Image.Image, rows: int, cols: int
) -> tuple[list[Image.Image], int, int]:
    width, height = image.size
    piece_width = width // cols
    piece_height = height // rows
    pieces = []

    for r in range(rows):
        for c in range(cols):
            box = (
                c * piece_width,
                r * piece_height,
                (c + 1) * piece_width,
                (r + 1) * piece_height,
            )
            pieces.append(image.crop(box))

    return pieces, piece_width, piece_height


def recolor_image(image: Image.Image, rows: int, cols: int) -> Image.Image:
    image = image.convert("RGB")
    total_average_color = get_average_color(image)
    pieces, piece_width, piece_height = split_image(image, rows, cols)

    diameter = min(pieces[0].size)
    radius = diameter // 2
    new_image = Image.new("RGB", image.size, total_average_color)

    for i, piece in enumerate(pieces):
        average_color = get_average_color(piece)
        row, col = divmod(i, cols)
        x = col * piece_width + piece_width // 2
        y = row * piece_height + piece_height // 2

        circle = Image.new("RGBA", (piece_width, piece_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(circle)
        draw.ellipse((0, 0, piece_width, piece_height), fill=average_color)
        new_image.paste(circle, (x - radius, y - radius), circle)

    new_image = new_image.filter(ImageFilter.SMOOTH)
    return new_image.filter(ImageFilter.GaussianBlur(50))


def create_gradient_image(
    size: Tuple[int, int], color1: Tuple[int, int, int], color2: Tuple[int, int, int]
) -> Image.Image:
    color1 = tuple(max(0, min(255, c)) for c in color1)
    color2 = tuple(max(0, min(255, c)) for c in color2)
    gradient_array = np.linspace(color1, color2, size[0])
    gradient_image = np.tile(gradient_array, (size[1], 1, 1)).astype(np.uint8)
    return Image.fromarray(gradient_image, "RGBA")


def create_vertical_gradient_rect(width, height, start_color, end_color):
    if width <= 0 or height <= 0:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    start_color = tuple(max(0, min(255, c)) for c in start_color)
    end_color = tuple(max(0, min(255, c)) for c in end_color)
    gradient_array = np.linspace(start_color, end_color, num=height, dtype=np.uint8)
    gradient_array = np.tile(gradient_array[:, np.newaxis, :], (1, width, 1))
    return Image.fromarray(gradient_array)


def random_color_offset(
    color: Tuple[int, int, int], offset: int
) -> Tuple[int, int, int]:
    return tuple(
        min(255, max(0, c + np.random.randint(-offset, offset + 1))) for c in color
    )


def get_brightest_and_darkest_color(
    image: Image.Image,
    saturation_threshold: int = 100,
    hue_difference_threshold: int = 30,
) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    img_hsv = np.array(image.convert("HSV"))
    vivid_mask = img_hsv[..., 1] > saturation_threshold
    vivid_pixels = img_hsv[vivid_mask]

    if len(vivid_pixels) < 10:
        if saturation_threshold <= 0:
            average = get_average_color(image)
            return average, average
        return get_brightest_and_darkest_color(image, saturation_threshold - 10)

    brightest_pixel = vivid_pixels[np.argmax(vivid_pixels[..., 2])]
    darkest_pixel = vivid_pixels[np.argmin(vivid_pixels[..., 2])]
    hue_difference = abs(int(brightest_pixel[0]) - int(darkest_pixel[0]))

    if hue_difference < hue_difference_threshold:
        possible_dark_pixels = vivid_pixels[vivid_pixels[..., 0] != brightest_pixel[0]]
        if len(possible_dark_pixels) > 0:
            darkest_pixel = possible_dark_pixels[
                np.argmin(possible_dark_pixels[..., 2])
            ]

    brightest_color = (
        Image.fromarray(np.uint8([[brightest_pixel]]), "HSV")
        .convert("RGB")
        .getpixel((0, 0))
    )
    darkest_color = (
        Image.fromarray(np.uint8([[darkest_pixel]]), "HSV")
        .convert("RGB")
        .getpixel((0, 0))
    )
    return brightest_color, darkest_color


def rounded_rectangle(
    image: Image.Image,
    radius: int,
    border=False,
    border_width=0,
    border_color=(0, 0, 0),
):
    width, height = image.size
    image_ = Image.new("RGBA", (width + 1, height + 1), (0, 0, 0, 0))
    image_.paste(image, (0, 0), image.convert("RGBA"))

    result = Image.new("RGBA", (width + 1, height + 1), (0, 0, 0, 0))
    mask = Image.new("L", (width + 1, height + 1), 0)
    draw = ImageDraw.Draw(mask)
    image_draw = ImageDraw.Draw(result)
    draw.rounded_rectangle((0, 0, width, height), radius=radius, fill=255)
    result.paste(image_, (0, 0), mask)

    if border:
        image_draw.rounded_rectangle(
            (0, 0, width, height),
            radius=radius,
            outline=border_color,
            width=border_width,
        )

    return result


def create_progress_bar(
    progress: float, color: Tuple[int, int, int], width=186, height=16
):
    progress = max(0, min(1, progress))
    color_hsv = rgb_to_hsv(*color)

    bar_color = tuple(
        map(int, hsv_to_rgb(color_hsv[0], color_hsv[1], color_hsv[2] * 0.8))
    )
    border_color = tuple(map(lambda x: max(x - 20, 0), color))
    border_image = rounded_rectangle(
        Image.new("RGBA", (width, height), bar_color),
        8,
        border=True,
        border_width=1,
        border_color=border_color,
    )

    bar_color_top = tuple(
        map(int, hsv_to_rgb(color_hsv[0], color_hsv[1] / 2, color_hsv[2] * 5 / 2))
    )
    bar_color_bottom = tuple(
        map(int, hsv_to_rgb(color_hsv[0], color_hsv[1] / 2, color_hsv[2]))
    )

    bar_image = create_vertical_gradient_rect(
        int(width * progress) - 6, height - 4, bar_color_top, bar_color_bottom
    )
    bar_image = rounded_rectangle(bar_image, 6)
    border_image.paste(bar_image, (3, 2), bar_image)
    return border_image


def draw_game_info(
    header: Image.Image,
    game_name: str,
    game_time: str,
    last_play_time: str,
    achievements: List[Dict[str, bytes]],
    completed_achievement_number: int,
    total_achievement_number: int,
    achievement_color: Tuple[int, int, int],
) -> Image.Image:
    bg = Image.new("RGBA", (880, 110 + 64 + 10), (0, 0, 0, 110))
    header = header.resize((229, 86), Image.BICUBIC)
    bg.paste(header, (10, 110 // 2 - header.height // 2))

    draw = ImageDraw.Draw(bg)
    draw.text(
        (260, 10),
        game_name,
        font=ImageFont.truetype(font_regular_path, 26),
        fill=(255, 255, 255),
    )

    font = ImageFont.truetype(font_light_path, 22)
    draw.text(
        (int(bg.width - font.getlength(last_play_time)) - 10, 75),
        last_play_time,
        font=font,
        fill=(150, 150, 150),
    )

    display_text = f"总时数 {game_time}"
    draw.text(
        (int(bg.width - font.getlength(display_text)) - 10, 50),
        display_text,
        font=font,
        fill=(150, 150, 150),
    )

    if completed_achievement_number is None or total_achievement_number is None:
        return bg.crop((0, 0, bg.width, 110))

    achievement_bg = Image.new("RGBA", (860, 64), achievement_color)
    draw_achievement = ImageDraw.Draw(achievement_bg)

    font = ImageFont.truetype(font_light_path, 18)
    x = 14
    draw_achievement.text(
        (x, 20),
        "成就进度",
        font=font,
        fill=(255, 255, 255, 255),
    )
    x += font.getlength("成就进度") + 10
    progress_text = f"{completed_achievement_number} / {total_achievement_number}"
    draw_achievement.text(
        (int(x), 20),
        progress_text,
        font=font,
        fill=(130, 130, 130),
    )
    x += font.getlength(progress_text) + 10
    progress_bar = create_progress_bar(
        completed_achievement_number / total_achievement_number, achievement_color
    )
    achievement_bg.paste(progress_bar, (int(x), 24), progress_bar)

    x = 860 - 48 * 6 - 10 * 6
    for achievement in achievements:
        achievement_image = Image.open(BytesIO(achievement["image"])).resize((48, 48))
        achievement_bg.paste(achievement_image, (x, 8))
        x += 48 + 10

    if completed_achievement_number > 6:
        font = ImageFont.truetype(font_regular_path, 22)
        display_text = f"+{completed_achievement_number - 5}"
        draw_achievement.rectangle((x, 8, x + 48, 56), fill=(34, 34, 34))
        draw_achievement.text(
            (x + 24 - font.getlength(display_text) // 2, 18),
            display_text,
            font=font,
            fill=(255, 255, 255),
        )

    bg.paste(achievement_bg, (10, 110), achievement_bg)
    return bg


def draw_player_status(
    player_bg: Image.Image,
    player_avatar: Image.Image,
    player_name: str,
    player_id: str,
    player_description: str,
    player_last_two_weeks_time: str,
    player_games: List[Dict[str, str]],
):
    if isinstance(player_bg, bytes):
        player_bg = Image.open(BytesIO(player_bg))
    if isinstance(player_avatar, bytes):
        player_avatar = Image.open(BytesIO(player_avatar))

    player_bg = player_bg.convert("RGBA")
    crop_left = max(0, (player_bg.width - 960) // 2)
    crop_right = min(player_bg.width, (player_bg.width + 960) // 2)
    cropped_bg = player_bg.crop((crop_left, 0, crop_right, player_bg.height))
    if cropped_bg.width != 960:
        cropped_bg = cropped_bg.resize((960, player_bg.height), Image.BICUBIC)

    bg = recolor_image(cropped_bg, 10, 10)
    enhancer = ImageEnhance.Brightness(bg)
    bg = enhancer.enhance(0.7)

    player_avatar = player_avatar.convert("RGBA").resize((200, 200))
    bg.paste(player_avatar, (40, 40), player_avatar)

    draw = ImageDraw.Draw(bg)
    draw.rectangle((40, 40, 240, 240), outline=(83, 164, 196), width=3)

    draw.text(
        (280, 48),
        player_name,
        font=ImageFont.truetype(font_light_path, 40),
        fill=(255, 255, 255),
    )
    draw.text(
        (280, 100),
        f"好友代码: {player_id}",
        font=ImageFont.truetype(font_regular_path, 19),
        fill=(191, 191, 191),
    )

    line_width = 0
    offset = 0
    line = ""
    description_font = ImageFont.truetype(font_light_path, 22)
    for idx, char in enumerate(player_description):
        line += char
        line_width += description_font.getlength(char)
        if line_width > 640 or idx == len(player_description) - 1 or char == "\n":
            draw.text(
                (280, 132 + offset),
                line,
                font=description_font,
                fill=(255, 255, 255),
            )
            line = ""
            offset += 25
            line_width = 0
        if offset >= 25 * 4:
            break

    brightest_color, darkest_color = get_brightest_and_darkest_color(player_bg)
    brightest_color = tuple(map(lambda x: x - 30 if x >= 30 else 0, brightest_color))
    darkest_color = tuple(
        map(lambda x: x + 30 if x <= 255 - 30 else 255, darkest_color)
    )
    brightest_color = (brightest_color[0], brightest_color[1], brightest_color[2], 128)
    brightest_color = random_color_offset(brightest_color, 20)
    darkest_color = (darkest_color[0], darkest_color[1], darkest_color[2], 128)
    darkest_color = random_color_offset(darkest_color, 20)

    hsv_achievement_color = rgb_to_hsv(*brightest_color[:3])
    achievement_color = tuple(
        map(
            int,
            hsv_to_rgb(
                hsv_achievement_color[0],
                hsv_achievement_color[1] * 0.85,
                hsv_achievement_color[2] * 0.6,
            ),
        )
    )

    game_images: List[Image.Image] = []
    for game in player_games:
        game_image = Image.open(BytesIO(game["game_header"]))
        game_info = draw_game_info(
            game_image,
            game["game_name"],
            game["game_time"],
            game["last_play_time"],
            game["achievements"],
            game["completed_achievement_number"],
            game["total_achievement_number"],
            achievement_color,
        )
        game_images.append(game_info)

    bg_game = Image.new(
        "RGBA", (920, 106 + sum([game_image.height + 26 for game_image in game_images]))
    )
    draw_game = ImageDraw.Draw(bg_game)
    draw_game.rectangle((0, 0, 920, bg_game.height), fill=(0, 0, 0, 120))
    bg.paste(bg_game, (20, 272), bg_game)

    gradient = create_gradient_image((920, 50), brightest_color, darkest_color)
    bg.paste(gradient, (20, 272), gradient)

    draw.text(
        (34, 279),
        "最新动态",
        font=ImageFont.truetype(font_light_path, 26),
        fill=(255, 255, 255),
    )
    if player_last_two_weeks_time is not None:
        font = ImageFont.truetype(font_light_path, 26)
        width = font.getlength(player_last_two_weeks_time)
        draw.text(
            (960 - width - 34, 279),
            player_last_two_weeks_time,
            font=font,
            fill=(255, 255, 255),
        )

    y = 350
    for game_image in game_images:
        bg.paste(
            game_image,
            ((920 - game_image.width) // 2 + 20, y),
            game_image.convert("RGBA"),
        )
        y += game_image.height + 26

    player_bg.paste(bg, ((player_bg.width - 960) // 2, 0), bg.convert("RGBA"))
    return player_bg.convert("RGB")

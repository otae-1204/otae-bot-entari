# from utils.image_utils import PILBuildImage
from configs.path_config import IMAGE_PATH, FONT_PATH
from pil_utils import BuildImage, Text2Image
from PIL import Image, ImageDraw, ImageFilter, ImageChops, ImageOps
from io import BytesIO
import re
import base64
import time

import datetime

path = IMAGE_PATH + "minecraft_plugin/"
ttf = "mc.ttf"

_S = 0.75
_FS = 0.78  # Skia 字体缩放系数（Skia 同数值比 PIL 渲染大 ~28%，1/1.28≈0.78）

# Minecraft 格式化代码 → 颜色映射
_MC_COLORS = {
    "0": "#000000", "1": "#0000AA", "2": "#00AA00", "3": "#00AAAA",
    "4": "#AA0000", "5": "#AA00AA", "6": "#FFAA00", "7": "#AAAAAA",
    "8": "#555555", "9": "#5555FF", "a": "#55FF55", "b": "#55FFFF",
    "c": "#FF5555", "d": "#FF55FF", "e": "#FFFF55", "f": "#FFFFFF",
}

# § 后跟的有效格式化字符（颜色 + 样式）
_MC_CODE_RE = re.compile(r"§[0-9a-fA-Fk-oK-OrR]")


def _motd_display_len(text: str) -> int:
    """返回去除 § 格式化代码后的可见字符数."""
    return len(_MC_CODE_RE.sub("", text))


def _motd_truncate(text: str, max_len: int) -> str:
    """按可见字符数截断文本，保留 § 格式化代码."""
    count = 0
    result = []
    i = 0
    while i < len(text) and count < max_len:
        if text[i] == "§" and i + 1 < len(text) and text[i + 1].lower() in "0123456789abcdefklmnor":
            result.append(text[i:i + 2])
            i += 2
        else:
            result.append(text[i])
            count += 1
            i += 1
    return "".join(result)


def _parse_motd_segments(text: str, default_color: str = "#ffffff") -> list[dict]:
    """解析含 § 格式化代码的文本为 {text, color} 段列表.
    样式代码（§k§l§m§n§o）忽略，§r 重置为默认颜色.
    """
    segments: list[dict] = []
    current_color = default_color
    buf: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "§" and i + 1 < len(text):
            code = text[i + 1].lower()
            if code in _MC_COLORS:
                if buf:
                    segments.append({"text": "".join(buf), "color": current_color})
                    buf = []
                current_color = _MC_COLORS[code]
                i += 2
            elif code == "r":
                if buf:
                    segments.append({"text": "".join(buf), "color": current_color})
                    buf = []
                current_color = default_color
                i += 2
            elif code in "klmno":
                # 样式代码当前忽略，不打断颜色段
                i += 2
            else:
                buf.append(text[i])
                i += 1
        else:
            buf.append(text[i])
            i += 1
    if buf:
        segments.append({"text": "".join(buf), "color": current_color})
    return segments


def _draw_motd_segments(img, x0: int, y: int, segments: list[dict],
                        font_families: list[str], font_size: int,
                        shadow: bool = False) -> int:
    """逐段绘制 MOTD 文本（支持颜色），可选阴影。返回结束 x 坐标."""
    x = x0
    for seg in segments:
        if not seg["text"]:
            continue
        if shadow:
            simg = Text2Image.from_text(
                seg["text"], font_families=font_families,
                font_size=font_size, fill="black",
            ).to_image()
            img.paste(simg, (x + 2, y + 2), alpha=True)
        timg = Text2Image.from_text(
            seg["text"], font_families=font_families,
            font_size=font_size, fill=seg["color"],
        ).to_image()
        img.paste(timg, (x, y), alpha=True)
        x += timg.width
    return x


def _motd_segments_width(segments: list[dict], font_families: list[str],
                         font_size: int) -> int:
    """计算所有段的总像素宽度."""
    w = 0
    for seg in segments:
        if not seg["text"]:
            continue
        timg = Text2Image.from_text(
            seg["text"], font_families=font_families,
            font_size=font_size, fill="white",
        ).to_image()
        w += timg.width
    return w


def draw_server_info(server_info: dict) -> BuildImage:
    """
    说明:
        绘制服务器信息
    参数:
        :param server_info: 服务器信息
            server_info = {
                "name": "服务器名称",
                "nickname": "服务器昵称",
                "address": "服务器地址",
                "status": "success",
                "data": {
                    "game_version": "游戏版本",
                    "is_vanilla": True,
                    "online_players": 10,
                    "max_players": 20,
                    "motd": "服务器MOTD",
                    "favicon": "服务器图标",
                    "server_type": "服务器类型",
                    "players": ["玩家1", "玩家2"],
                    "latency": 50
                }
            }
    返回:
        :return: BuildImage 对象
    """
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]正在绘制服务器: {server_info.get('name')}")
    # 预计算 MOTD 行高（保留 § 格式化代码，按可见字符数截断）
    _data = server_info.get("data", {})
    if not isinstance(_data, dict):
        _data = {}
    _motd_raw = _data.get("motd", "")
    if not isinstance(_motd_raw, str):
        _motd_raw = ""
    _motd_raw = _motd_raw.replace("\r", "")
    _raw_lines = [line for line in _motd_raw.split("\n")[:2] if _motd_display_len(line.strip()) > 0]
    _motd_font_size = int(145 * _FS)
    _motd_line_spacing = 155  # MOTD 行间距（像素），Skia 渲染 ~145px 高的字体留 10px 间距
    _motd_segments_per_line = [
        _parse_motd_segments(_motd_truncate(line, 40), "#808080")
        for line in _raw_lines
    ]
    _extra_h = max(0, (len(_raw_lines) - 1)) * _motd_line_spacing if _raw_lines else 0
    _card_h = int(640 * _S) + _extra_h
    # 创建图片（高度含多行 MOTD）
    img = BuildImage.new("RGBA", (int(5760 * _S), _card_h), (0, 0, 0, 0))
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]创建图片对象完成")
    # 获取服务器名称的显示文本
    nickname = server_info.get("nickname")
    if nickname and isinstance(nickname, list) and len(nickname) > 0:
        nickname_str = ", ".join(nickname)
        name_text = f"{server_info.get('name', '')}({nickname_str})"
    elif nickname and isinstance(nickname, str):
        name_text = f"{server_info.get('name', '')}({nickname})"
    else:
        name_text = server_info.get("name", '')
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]获取服务器名称完成: {name_text}")

    # 判断名称是否超过上限,超过则不显示昵称
    if len(name_text) > 40:
        name_text = server_info.get("name",'')
      
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]开始绘制服务器名称: {name_text}")
    # 绘制服务器名称
    img.draw_text(
        xy=(int(690*_S), 0),
        text=name_text,
        font_families=['Minecraft AE Pixel'],
        font_size=int(165*_FS),
        fill="white",
        halign="left",
                    )
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]绘制服务器名称完成")

    # 绘制服务器地址
    img.draw_text(
        xy=(int(690*_S), int(280*_S)),
        text=f"Address: {server_info.get('address')}",
        font_families=['Minecraft AE Pixel'],
        font_size=int(145*_FS),
        fill="#aaaaaa",
        halign="left",
                    )
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]绘制服务器地址完成")

    # 确定icon图片
    icon_path = get_server_icon(server_info)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]获取服务器图标完成: {icon_path}")

    # 绘制icon
    icon = BuildImage.open(icon_path)
    icon = icon.resize(size=(int(640*_S), int(640*_S)), keep_ratio=True)
    img.paste(icon, (0, 0), alpha=True)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]绘制服务器图标完成")

    # 判断延迟档位(0-200+分为四档)
    latency_img = draw_latency_icon(server_info)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]绘制延迟图标完成: {latency_img}")

    # 绘制延迟
    latency = BuildImage.open(latency_img)
    # 调整大小
    latency = latency.resize(size=(int(200*_S), int(160*_S)), keep_ratio=True)
    # 粘贴
    img.paste(latency, (int(5760*_S)-int(230*_S), 0), alpha=True)

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]粘贴延迟图标完成")

    # 如果服务器未开启则绘制错误信息
    if server_info["status"] != "success":
        img.draw_text(
            xy=(int(690*_S), int(455*_S)),
            text=f"无法连接到服务器",
            font_families=['Minecraft AE Pixel'],
            font_size=int(145*_FS),
            fill="#fc0000",
            halign="left",
                    )
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]绘制错误信息完成")
        return img
        # output: ByteIo
        # output = img.save_png()

        # return base64.b64encode(output.getvalue()).decode()

    # 绘制服务器motd（多行时向下扩展，逐段着色）
    _motd_x = int(690 * _S)
    _motd_prefix_w = 0
    if _motd_segments_per_line:
        _motd_prefix_img = Text2Image.from_text(
            "Motd: ",
            font_families=['Minecraft AE Pixel'],
            font_size=_motd_font_size,
            fill="#808080",
        ).to_image()
        _motd_prefix_w = _motd_prefix_img.width
        img.paste(_motd_prefix_img, (_motd_x, int(455 * _S)), alpha=True)
    for i, segments in enumerate(_motd_segments_per_line):
        _draw_motd_segments(
            img, _motd_x + _motd_prefix_w, int(455 * _S) + i * _motd_line_spacing,
            segments, ['Minecraft AE Pixel'], _motd_font_size,
        )


    # 绘制服务器在线玩家数量
    online_players = f"{server_info['data'].get('online_players')}/{server_info['data'].get('max_players')}"
    op_img = Text2Image.from_text(
        text=online_players,
        font_families=['Minecraft AE Pixel'],
        font_size=int(145*_FS),
        fill="#aaaaaa",
            ).to_image()
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]绘制在线玩家数量完成: {online_players}")

    # 粘贴
    img.paste(op_img, (int(5760*_S)-int(260*_S)-op_img.width, int(30*_S)), alpha=True)

    # 绘制版本信息
    v_img = Text2Image.from_text(
        text=server_info['data'].get('game_version'),
        font_families=['Minecraft AE Pixel'],
        font_size=int(145*_FS),
        fill="#aaaaaa",
            ).to_image()
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]绘制版本信息完成: {server_info['data'].get('game_version')}")

    # 粘贴
    img.paste(v_img, (int(5760*_S)-int(30*_S)-v_img.width, int(645*_S)-int(40*_S)-v_img.height), alpha=True)

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]绘制完成")
    return img


def draw_server_list(server_info_imgs: list[BuildImage], group_name: str) -> BytesIO:
    """
    说明:
        绘制服务器列表
    参数:
        :param server_info_imgs: 服务器信息图片列表
        :param group_name: 群名
    返回:
        :return: Base64编码的图像数据
    """
    print("开始绘制服务器列表")
    server_len = len(server_info_imgs)
    gap = int(85 * _S)
    top_pad = int(640 * _S) + int(60 * _S)  # 标题栏高度 + 上间距
    bottom_pad = int(640 * _S) + int(60 * _S)

    # 根据实际卡片高度计算总高
    card_heights = [img.height for img in server_info_imgs]
    total_cards_h = sum(card_heights) + gap * max(0, server_len - 1)
    img_y = top_pad + total_cards_h + bottom_pad
    img_x = int(7680 * _S)

    img = BuildImage.new("RGBA", (img_x, img_y), (0, 0, 0, 0))

    # 粘贴背景（tile）
    bg = BuildImage.open(path + "body_bg.png")
    bg = bg.resize(size=(img_x, int(640 * _S)), keep_ratio=True)
    bg_step = int(640 * _S)
    for i in range(0, img_y, bg_step):
        img.paste(bg, (0, i), alpha=True)

    # 粘贴上下边框
    border = BuildImage.open(path + "head_bg.png")
    border = border.resize(size=(img_x, int(640 * _S)), keep_ratio=True)
    img.paste(border, (0, 0), alpha=True)
    img.paste(border, (0, img_y - int(640 * _S)), alpha=True)

    # 绘制群名
    group_name_img = Text2Image.from_text(
        text=group_name,
        font_families=['Minecraft AE Pixel'],
        font_size=int(220*_FS),
        fill="white",
            ).to_image()
    img.paste(group_name_img, (int(3840*_S) - int(group_name_img.width/2), int(250*_S)), alpha=True)

    # 绘制服务器列表（可变高度）
    y_cursor = top_pad
    for server_info_img in server_info_imgs:
        img.paste(server_info_img, (int(960*_S), y_cursor), alpha=True)
        y_cursor += server_info_img.height + gap

    # # 绘制服务器列表
    # for i, server_info_img in enumerate(server_info_imgs):
    #     img.paste(server_info_img, (0, 640*i), alpha=True)

    # output: ByteIo
    output = img.save_png()
    return output

    # return base64.b64encode(output.getvalue()).decode()


def draw_server_players(server_info: dict) -> BytesIO:
    """
    说明:
        绘制服务器详细信息
    参数:
        :param server_info: 服务器信息
            server_info = {
                "name": "服务器名称",
                "nickname": "服务器昵称",
                "address": "服务器地址",
                "status": "success",
                "data": {
                    "game_version": "游戏版本",
                    "is_vanilla": True,
                    "online_players": 10,
                    "max_players": 20,
                    "motd": "服务器MOTD",
                    "favicon": "服务器图标",
                    "server_type": "服务器类型",
                    "players": ["玩家1", "玩家2"],
                    "latency": 50
                }
            }
    返回:
        Base64编码的图像数据
    """
    times = time.time()
    # 获取玩家数量
    data = server_info.get("data", {})
    if not isinstance(data, dict):
        data = {}
    players_hidden = bool(data.get("players_hidden"))
    player_names = data.get("players")
    if player_names is None:
        player_names = []
    if players_hidden:
        online_count = int(data.get("online_players", 0) or 0)
        player_names = [f"玩家列表已隐藏 ({online_count} 在线)"]

    player_count = len(player_names) if len(player_names) >= 3 else 3

    # 提前创建服务器名称和IP的Text2Image对象
    server_name = server_info.get("name",'')
    server_name_img = Text2Image.from_text(
        text=server_name,
        font_families=['Minecraft AE Pixel'],
        font_size=int(48*_FS),
        fill="white",
            ).to_image()

    server_address_img = Text2Image.from_text(
        text="IP: " + server_info.get("address",''),
        font_families=['Minecraft AE Pixel'],
        font_size=int(28*_FS),
        fill="white",
            ).to_image()

    max_len = max(server_name_img.width, server_address_img.width)

    # 预先计算 MOTD 行数（用于撑高信息框，保留 § 格式化代码）
    _motd_segments_per_line: list[list[dict]] = []
    if server_info["status"] == "success":
        _data = server_info.get("data", {})
        if not isinstance(_data, dict):
            _data = {}
        raw_motd = _data.get("motd", "")
        if not isinstance(raw_motd, str):
            raw_motd = ""
        raw_motd = raw_motd.replace("\r", "")
        for line in raw_motd.split("\n")[:3]:
            truncated = _motd_truncate(line, 25)
            if _motd_display_len(truncated.strip()) > 0:
                _motd_segments_per_line.append(
                    _parse_motd_segments(truncated, "#ffffff")
                )
    _motd_line_h = int(28 * _FS)
    _motd_extra = max(0, (len(_motd_segments_per_line) - 1)) * (_motd_line_h + 4) if _motd_segments_per_line else 0

    # 计算服务器信息区域大小
    server_info_width = max_len + 200
    server_info_height = 200 + _motd_extra

    # 玩家列表区域大小
    player_list_height = player_count * 40 + 25*2  # 20为边框宽度

    info_area_width = server_info_width + 12*2  # 12为边框宽度
    info_area_height = server_info_height + player_list_height + 12*2  # 12为边框宽度

    width = info_area_width + 192
    height = info_area_height + 192

    # # 设置总体图片大小(服务器信息区域 + 边框 + 外间距)
    # width = server_info_width + 96 + 96
    # height = server_info_height + 96 + 96 + player_count * 40 + 30

    # # 设置信息区域大小
    # info_area_width = width - 192 - 12*2  # 12为边框宽度
    # info_area_height = height - 192 - 12*2  # 12为边框宽度



    # 创建一个新的图片对象
    img = BuildImage.new("RGBA", (width, height), (0, 0, 0, 0))

    # 绘制背景
    bg = BuildImage.open(path + "info_body_bg.png")
    bg = bg.resize(size=(width, height), keep_ratio=True)

    img.paste(bg, (0, 0), alpha=True)

    # 绘制信息框
    window_frame = draw_mc_style_rounded_rect(
        width=info_area_width,
        height=info_area_height,
        radius=8,
        light_color=(255, 255, 255, 255),  # 亮色部分
        dark_color=(85, 85, 85, 255),   # 暗色部分
        bg_color=(198, 198, 198, 255),        # 内部背景色（半透明灰色）
        border_width=8,                   # 3像素宽的边框
        outline_color=(0, 0, 0, 255),  # 黑色描边
        outline_width=4,                # 不需要外描边
    )

    # 绘制服务器图标
    if server_info["status"] == "success" and server_info["data"].get("favicon") is not None:
        icon_data = server_info["data"].get("favicon")
        icon_data = base64.b64decode(icon_data)
        icon = BytesIO(icon_data)
    else:
        icon = path + "unknown_server.png"

    # 绘制icon
    icon = BuildImage.open(icon)
    icon = icon.resize(size=(64, 64), keep_ratio=True)
    # 给icon加边框
    icon_bg = draw_mc_style_rounded_rect(
        width=66,
        height=66,
        radius=0,
        light_color=(255, 255, 255, 255),  # 亮色部分
        dark_color=(85, 85, 85, 255),   # 暗色部分
        bg_color=(0, 0, 0, 0),  
        border_width=1,               # 3像素宽的边框
        outline_width=0,              # 不需要外描边
    )
    icon_bg.paste(icon, (1, 1), alpha=True)

    # 粘贴icon
    window_frame.paste(icon_bg, (48, 38), alpha=True)

    server_name = server_info.get("name",'')
    # 绘制服务器名称阴影
    window_frame.draw_text(
        xy=(137, 52),
        text=server_name,
        font_families=['Minecraft AE Pixel'],
        font_size=int(48*_FS),
        fill="black",
        halign="left"
    )
    # 绘制服务器名称
    window_frame.paste(server_name_img, (135, 50), alpha=True)
    # window_frame.draw_text(
    #     xy=(135, 50),
    #     text=server_name,
    #     font_families=['Minecraft AE Pixel'],
    #     font_size=int(48*_FS),
    #     fill="white",
    #     halign="left"
    # )

    # 绘制服务器地址

    server_address = "IP: " + server_info.get("address",'')
    # 绘制服务器地址阴影
    window_frame.draw_text(
        xy=(55, 117),
        text=server_address,
        font_families=['Minecraft AE Pixel'],
        font_size=int(28*_FS),
        fill="black",
        halign="left"
    )
    # 绘制服务器地址
    # window_frame.paste(server_address_img, (53, 115), alpha=True)
    window_frame.draw_text(
        xy=(53, 115),
        text=server_address,
        font_families=['Minecraft AE Pixel'],
        font_size=int(28*_FS),
        fill="white",
        halign="left"
    )

    # 绘制Motd（第一行带 "Motd: " 前缀 + 阴影，逐段着色）
    _motd_prefix_w = 0
    _pfx_img = None
    if _motd_segments_per_line and server_info["status"] == "success":
        _pfx_img = Text2Image.from_text(
            "Motd: ", font_families=['Minecraft AE Pixel'],
            font_size=_motd_line_h, fill="white",
        ).to_image()
        _motd_prefix_w = _pfx_img.width

    if server_info["status"] == "success" and _motd_segments_per_line:
        for i, segments in enumerate(_motd_segments_per_line):
            y_off = i * (_motd_line_h + 4)
            y_base = 156 + y_off
            if i == 0:
                # "Motd: " 前缀（带阴影）
                _pfx_shadow = Text2Image.from_text(
                    "Motd: ", font_families=['Minecraft AE Pixel'],
                    font_size=_motd_line_h, fill="black",
                ).to_image()
                window_frame.paste(_pfx_shadow, (55, y_base + 2), alpha=True)
                window_frame.paste(_pfx_img, (53, y_base), alpha=True)
                x_start = 53 + _motd_prefix_w
            else:
                x_start = 53 + _motd_prefix_w
            _draw_motd_segments(
                window_frame, x_start, y_base,
                segments, ['Minecraft AE Pixel'], _motd_line_h,
                shadow=True,
            )
    elif server_info["status"] != "success":
        window_frame.draw_text(
            xy=(55, 158), text="Failed to connect to server",
            font_families=['Minecraft AE Pixel'], font_size=_motd_line_h,
            fill="black", halign="left",
        )
        window_frame.draw_text(
            xy=(53, 156), text="Failed to connect to server",
            font_families=['Minecraft AE Pixel'], font_size=_motd_line_h,
            fill="white", halign="left",
        )

    # 绘制玩家列表框
    # 绘制玩家列表框
    player_frame = draw_mc_style_rounded_rect(
        width=info_area_width - 24*2,
        height=player_list_height,
        radius=0,
        light_color=(32, 32, 32, 255),
        dark_color=(148, 148, 148, 255),
        bg_color=(110, 110, 110, 255),
        border_width=2,               # 增加边框宽度使其更明显
        outline_width=0,              # 不需要外描边
        shadow_width=9,            # 增加阴影宽度使其更明显
        shadow_color=(20, 20, 20, 64)   # 增加阴影颜色使其更明显
    )

    # 粘贴玩家列表框
    window_frame.paste(player_frame, (28, server_info_height), alpha=True)

    # 绘制玩家列表
    for i, player_name in enumerate(player_names):
        # 绘制玩家名称阴影
        window_frame.draw_text(
            xy=(72, server_info_height + 32 + i*40),
            text=player_name,
            font_families=['Minecraft AE Pixel'],
            font_size=int(30*_FS),
            fill="black",
            halign="left"
        )
        # 绘制玩家名称
        window_frame.draw_text(
            xy=(70, server_info_height + 30 + i*40),
            text=player_name,
            font_families=['Minecraft AE Pixel'],
            font_size=int(30*_FS),
            fill="white",
            halign="left"
        )



    # 粘贴总信息框
    img.paste(window_frame, (96, 96), alpha=True)

    # 保存图片
    output = img.save_png()
    print("绘制时间:", time.time()-times)
    return output


def add_inner_shadow_to_image(
        image, 
        shadow_width=5,
        shadow_color=(0, 0, 0, 100),
        radius=0,
        border_width=0
    ):
    """
    为图像添加内阴影效果
    
    参数:
        image: BuildImage对象
        shadow_width: 阴影宽度
        shadow_color: 阴影颜色(RGBA)
        radius: 圆角半径
        border_width: 边框宽度(如果有边框，内阴影需要从边框内开始)
    
    返回:
        带有内阴影的BuildImage对象
    """
    # 获取图像尺寸
    width, height = image.size
    
    if shadow_width <= 0:
        # 如果阴影宽度为0或负数，直接返回原图
        return image
    
    # 创建阴影遮罩
    shadow_mask = Image.new("L", (width, height), 0)
    shadow_draw = ImageDraw.Draw(shadow_mask)
    
    # 绘制外部填充区域
    outer_rect = [(border_width, border_width), 
                  (width-border_width-1, height-border_width-1)]
    shadow_draw.rounded_rectangle(
        outer_rect,
        radius=max(0, radius-border_width),
        fill=255
    )
    
    # 绘制内部不需要阴影的区域
    inner_shadow_rect = [
        (border_width + shadow_width, border_width + shadow_width),
        (width-border_width-shadow_width-1, height-border_width-shadow_width-1)
    ]
    shadow_draw.rounded_rectangle(
        inner_shadow_rect,
        radius=max(0, radius-border_width-shadow_width),
        fill=0
    )
    
    # 模糊阴影边缘
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(shadow_width/2))
    
    # 创建阴影层
    shadow_layer = Image.new("RGBA", (width, height), shadow_color)
    shadow_layer.putalpha(shadow_mask)
    
    # 将阴影层叠加到原图
    result_img = BuildImage.new("RGBA", (width, height), (0, 0, 0, 0))
    result_img.image = Image.alpha_composite(image.image, shadow_layer)
    
    return result_img


# 获取服务器图标
def get_server_icon(server_info: dict) -> str | BytesIO:
    """
    说明:
        获取服务器图标
    参数:
        :param server_info: 服务器信息
            server_info = {
                "name": "服务器名称",
                "nickname": "服务器昵称",
                "address": "服务器地址",
                "status": "success",
                "data": {
                    "game_version": "游戏版本",
                    "is_vanilla": True,
                    "online_players": 10,
                    "max_players": 20,
                    "motd": "服务器MOTD",
                    "favicon": "服务器图标",
                    "server_type": "服务器类型",
                    "players": ["玩家1", "玩家2"],
                    "latency": 50,
                    "protocol": 123
                }
            }
    返回:
        :return: 对应的服务器图标路径
    """
    # 判断服务器状态
    try:
        if server_info["status"] == "success" and server_info["data"].get("favicon") is not None:
            icon_data = server_info["data"].get("favicon")
            icon_data = base64.b64decode(icon_data)
            icon = BytesIO(icon_data)
        else:
            icon = path + "unknown_server.png"
    except Exception as e:
        icon = path + "unknown_server.png"
    return icon


# 根据不同延迟档位获取延迟图标
def draw_latency_icon(server_info: dict) -> str:
    """
    说明:
        根据延迟档位绘制延迟图标
    参数:
        :server_info : 服务器信息
            server_info = {
                "name": "服务器名称",
                "nickname": "服务器昵称",
                "address": "服务器地址",
                "status": "success",
                "data": {
                    "game_version": "游戏版本",
                    "is_vanilla": True,
                    "online_players": 10,
                    "max_players": 20,
                    "motd": "服务器MOTD",
                    "favicon": "服务器图标",
                    "server_type": "服务器类型",
                    "players": ["玩家1", "玩家2"],
                    "latency": 50
                }
    返回:
        :return: 对应的延迟图标路径
    """
    # 判断延迟档位(0-200+分为四档)
    if server_info["status"] == "success":
        latency_num = server_info["data"].get("latency")
        if latency_num < 50:
            latency_img = path + "latency_5.png"
        elif latency_num < 100:
            latency_img = path + "latency_4.png"
        elif latency_num < 150:
            latency_img = path + "latency_3.png"
        elif latency_num < 200:
            latency_img = path + "latency_2.png"
        else:
            latency_img = path + "latency_1.png"
    else:
        latency_img = path + "latency_unknown.png"
    return latency_img


def draw_mc_style_rounded_rect(width, height, radius, light_color, dark_color, bg_color=(0, 0, 0, 0),
                               border_width=1, outline_color=(0, 0, 0, 255), outline_width=0, shadow_width=0,
                               shadow_color=(0, 0, 0, 100)):
    """
    绘制Minecraft风格的双色圆角矩形（颜色交界处沿角平分线，带黑色描边和内阴影）

    参数:
        width: 宽度
        height: 高度
        radius: 圆角半径
        light_color: 亮色部分颜色 (上边和左边)
        dark_color: 暗色部分颜色 (右边和下边) 
        bg_color: 背景颜色 (None表示透明)
        border_width: 边框宽度，默认为1
        outline_color: 描边颜色，默认为黑色
        outline_width: 描边宽度，默认为1
    """

    # 如果需要描边，则需要创建一个更大的图像
    if outline_width > 0:
        # 创建一个新的图像对象（包含描边）
        # 创建包含描边的完整图像
        total_width = width + outline_width * 2
        total_height = height + outline_width * 2
        total_img = BuildImage.new(
            "RGBA", (total_width, total_height), (0, 0, 0, 0))

        # 绘制黑色描边底层
        outline_img = BuildImage.new(
            "RGBA", (total_width, total_height), (0, 0, 0, 0))
        outline_draw = outline_img.draw
        outline_draw.rounded_rectangle(
            [(0, 0), (total_width-1, total_height-1)],
            radius=radius+outline_width,
            fill=outline_color
        )
        total_img.paste(outline_img, (0, 0), alpha=True)

        # 创建内部圆角矩形（调整尺寸）
        inner_img = draw_mc_style_rounded_rect_core(
            width, height, radius, light_color, dark_color, bg_color, border_width, shadow_width, shadow_color)

        # 将内部矩形粘贴到描边图像上
        total_img.paste(inner_img, (outline_width, outline_width), alpha=True)

        return total_img
    else:
        # 不需要描边，直接调用核心绘制函数
        result_img = draw_mc_style_rounded_rect_core(
            width, height, radius, light_color, dark_color, bg_color, border_width, shadow_width, shadow_color)
            
        return result_img


def draw_mc_style_rounded_rect_core(width, height, radius, light_color, 
                                    dark_color, bg_color=(0, 0, 0, 0), border_width=1,
                                    shadow_width=0, shadow_color=(0, 0, 0, 100)):
    """核心绘制函数 - 不含描边的圆角矩形"""
    from PIL import Image, ImageDraw

    # 创建一个新的图片对象（最终结果）
    img = BuildImage.new("RGBA", (width, height), (0, 0, 0, 0))

    # 计算边框和内部圆角的准确尺寸
    outer_radius = radius
    inner_radius = max(0, radius - border_width)

    # 1. 先创建完整的背景填充
    if bg_color is not None:
        # 创建背景图层
        bg_img = BuildImage.new("RGBA", (width, height), (0, 0, 0, 0))
        bg_draw = bg_img.draw

        # 计算内部填充的精确边界
        inner_rect = [
            (border_width, border_width),
            (width-border_width-1, height-border_width-1)
        ]

        # 绘制内部填充
        bg_draw.rounded_rectangle(
            inner_rect,
            radius=inner_radius,
            fill=bg_color
        )

        # 将背景层粘贴到结果图像
        img.paste(bg_img, (0, 0), alpha=True)

    # 如果有内阴影效果，则需要在背景上绘制阴影
    if shadow_width > 0:
        # 添加内阴影效果
        shadow_img = add_inner_shadow_to_image(
            img, shadow_width=shadow_width, shadow_color=shadow_color, radius=radius, border_width=border_width)
        img.paste(shadow_img, (0, 0), alpha=True)

    # 2. 创建边框遮罩图像
    border_mask = BuildImage.new("RGBA", (width, height), (0, 0, 0, 0))
    mask_draw = border_mask.draw

    # 绘制外轮廓
    mask_draw.rounded_rectangle(
        [(0, 0), (width-1, height-1)],
        radius=outer_radius,
        fill=(255, 255, 255, 255)
    )

    # 如果有背景色，绘制内轮廓（挖空中间）
    if bg_color is not None:
        inner_rect = [
            (border_width, border_width),
            (width-border_width-1, height-border_width-1)
        ]
        mask_draw.rounded_rectangle(
            inner_rect,
            radius=inner_radius,
            fill=(0, 0, 0, 0)
        )

    # 3. 创建亮色和暗色部分
    light_part = BuildImage.new("RGBA", (width, height), light_color)
    dark_part = BuildImage.new("RGBA", (width, height), dark_color)

    # 4. 创建角平分线分割的遮罩（替换原来的对角线遮罩）
    diagonal_mask = Image.new("L", (width, height), 0)
    diag_draw = ImageDraw.Draw(diagonal_mask)

    # 根据角平分线绘制亮色区域
    # 顶部边框 - 完全是亮色
    diag_draw.rectangle(
        [(0, 0), (width-1, border_width-1)],
        fill=255
    )
    
    # 左侧边框 - 完全是亮色
    diag_draw.rectangle(
        [(0, border_width), (border_width-1, height-1)],
        fill=255
    )
    
    # 根据是否有圆角使用不同的角平分线处理方法
    if radius <= 0:
        # 非圆角情况 - 使用精确的45度角平分线绘制
        
        # 右上角 - 精确绘制45度角平分线
        for i in range(border_width + 1):
            # 从右上角边框交点到顶部边框交点画线
            diag_draw.line(
                [(width - border_width + i, 0), (width, i)],
                fill=255,
                width=1
            )
        
        # 左下角 - 精确绘制45度角平分线
        for i in range(border_width + 1):
            # 从左下角边框交点到底部边框交点画线
            diag_draw.line(
                [(0, height - border_width + i), (i, height)],
                fill=255,
                width=1
            )
    else:
        # 有圆角情况 - 使用之前的处理方法
        # 右上角区域 - 按角平分线分割，扩大覆盖范围
        rt_points = [
            (width-border_width-5, 0),      # 右上边框交点(稍微扩展)
            (width, 0),                     # 右上角
            (width, border_width+5),        # 上右边框交点(稍微扩展)
        ]
        diag_draw.polygon(rt_points, fill=255)
        
        # 左下角区域 - 按角平分线分割，扩大覆盖范围
        lb_points = [
            (0, height-border_width-5),     # 左下边框交点(稍微扩展)
            (0, height),                    # 左下角
            (border_width+5, height),       # 下左边框交点(稍微扩展)
        ]
        diag_draw.polygon(lb_points, fill=255)
        
        # 为保证边界平滑，额外添加两个填充区域
        if border_width > 3:
            # 添加右上角额外填充
            diag_draw.rectangle(
                [(width-border_width, 0), (width, border_width)],
                fill=255
            )
            
            # 添加左下角额外填充
            diag_draw.rectangle(
                [(0, height-border_width), (border_width, height)],
                fill=255
            )
        
        # 处理圆角区域
        # 左上角 - 完全亮色
        diag_draw.pieslice(
            [(0, 0), (radius*2-1, radius*2-1)],
            180, 270, fill=255
        )
        
        # 右上角 - 角平分线分割
        rt_corner = Image.new("L", (radius*2, radius*2), 0)
        rt_draw = ImageDraw.Draw(rt_corner)
        
        # 扩大填充区域：从270度到320度(比原来的315度多5度)
        rt_draw.pieslice(
            [(0, 0), (radius*2-1, radius*2-1)],
            270, 320, fill=255  # 扩大圆弧范围
        )
        
        # 额外添加一个小矩形确保与顶部边框无缝连接
        rt_draw.rectangle(
            [(0, 0), (radius, border_width)],
            fill=255
        )
        
        # 将右上角圆角区域粘贴到遮罩上
        diagonal_mask.paste(rt_corner, (width-radius*2, 0))
        
        # 左下角 - 角平分线分割
        lb_corner = Image.new("L", (radius*2, radius*2), 0)
        lb_draw = ImageDraw.Draw(lb_corner)
        
        # 扩大填充区域：从130度到180度
        lb_draw.pieslice(
            [(0, 0), (radius*2-1, radius*2-1)],
            130, 180, fill=255  # 扩大圆弧范围
        )
        
        # 额外添加一个小矩形确保与左侧边框无缝连接
        lb_draw.rectangle(
            [(0, 0), (border_width, radius)],
            fill=255
        )
        
        # 将左下角圆角区域粘贴到遮罩上
        diagonal_mask.paste(lb_corner, (0, height-radius*2))

    # 5. 应用角平分线遮罩到亮色和暗色部分
    pil_light = light_part.image.copy()
    pil_dark = dark_part.image.copy()

    # 应用对角线遮罩
    pil_light.putalpha(Image.composite(
        Image.new("L", (width, height), 255),
        Image.new("L", (width, height), 0),
        diagonal_mask
    ))

    pil_dark.putalpha(Image.composite(
        Image.new("L", (width, height), 0),
        Image.new("L", (width, height), 255),
        diagonal_mask
    ))

    # 6. 将亮色和暗色部分合并
    combined = Image.alpha_composite(pil_dark, pil_light)

    # 7. 使用边框遮罩提取边框
    border_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    border_img = Image.composite(
        combined,
        Image.new("RGBA", (width, height), (0, 0, 0, 0)),
        border_mask.image.split()[3]  # 使用Alpha通道作为遮罩
    )

    # 8. 将边框粘贴到结果图像上（已经包含背景）
    border_build_img = BuildImage.new("RGBA", (width, height), (0, 0, 0, 0))
    border_build_img.image = border_img
    img.paste(border_build_img, (0, 0), alpha=True)

    return img


def draw_player_leaderboard(server_info: dict) -> BytesIO:
    """绘制在线玩家排行榜（Minecraft 风格窗口）"""
    data = server_info.get("data", {})
    if not isinstance(data, dict):
        data = {}
    players_hidden = bool(data.get("players_hidden"))
    player_names = data.get("players") or []
    online = data.get("online_players", len(player_names))
    latency = data.get("latency", 0)
    version = data.get("game_version", "")
    server_name = server_info.get("name", "未知服务器")
    address = server_info.get("address", "")

    display_count = max(len(player_names), 3)
    ROW_H = 42
    HEADER_H = 120
    FOOTER_H = 40
    PAD_X = 32
    PAD_Y = 12
    CARD_W = 680
    CARD_INNER_W = CARD_W - PAD_X * 2
    content_h = HEADER_H + display_count * ROW_H + FOOTER_H
    card_h = content_h + PAD_Y * 2
    total_w = CARD_W + 192
    total_h = card_h + 192

    img = BuildImage.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    bg = BuildImage.open(path + "info_body_bg.png")
    bg = bg.resize(size=(total_w, total_h), keep_ratio=True)
    img.paste(bg, (0, 0), alpha=True)

    window = draw_mc_style_rounded_rect(
        width=CARD_W, height=card_h, radius=8,
        light_color=(255, 255, 255, 255), dark_color=(85, 85, 85, 255),
        bg_color=(198, 198, 198, 255), border_width=8,
        outline_color=(0, 0, 0, 255), outline_width=4,
    )

    icon_size = 64
    if server_info.get("status") == "success" and data.get("favicon"):
        try:
            icon_data = base64.b64decode(data["favicon"])
            icon = BuildImage.open(BytesIO(icon_data))
        except Exception:
            icon = BuildImage.open(path + "unknown_server.png")
    else:
        icon = BuildImage.open(path + "unknown_server.png")
    icon = icon.resize(size=(icon_size, icon_size), keep_ratio=True)

    icon_frame = draw_mc_style_rounded_rect(
        width=icon_size + 4, height=icon_size + 4, radius=0,
        light_color=(255, 255, 255, 255), dark_color=(85, 85, 85, 255),
        bg_color=(0, 0, 0, 0), border_width=1, outline_width=0,
    )
    icon_frame.paste(icon, (2, 2), alpha=True)
    window.paste(icon_frame, (36, 30), alpha=True)

    title_x = 36 + icon_size + 20
    window.draw_text((title_x + 2, 42), server_name,
                     font_families=['Minecraft AE Pixel'], font_size=int(48 * _FS),
                     fill="black", halign="left")
    window.draw_text((title_x, 40), server_name,
                     font_families=['Minecraft AE Pixel'], font_size=int(48 * _FS),
                     fill="white", halign="left")

    if address:
        subtitle = f"IP: {address}"
        if version:
            subtitle += f"  |  {version}"
        if latency:
            subtitle += f"  |  {latency}ms"
        window.draw_text((title_x + 2, 92), subtitle,
                         font_families=['Minecraft AE Pixel'], font_size=int(24 * _FS),
                         fill="black", halign="left")
        window.draw_text((title_x, 90), subtitle,
                         font_families=['Minecraft AE Pixel'], font_size=int(24 * _FS),
                         fill="#cccccc", halign="left")

    line_y = HEADER_H
    window.draw_line((36, line_y, CARD_W - 36, line_y), fill=(0, 0, 0, 80), width=2)
    window.draw_line((36, line_y + 1, CARD_W - 36, line_y + 1), fill=(255, 255, 255, 60), width=1)

    rank_hdr_y = line_y + 12
    window.draw_text((60, rank_hdr_y), "排名",
                     font_families=['Minecraft AE Pixel'], font_size=int(22 * _FS),
                     fill=(80, 80, 80, 255), halign="left")
    window.draw_text((120, rank_hdr_y), "玩家名称",
                     font_families=['Minecraft AE Pixel'], font_size=int(22 * _FS),
                     fill=(80, 80, 80, 255), halign="left")
    _time_hdr_img = Text2Image.from_text(
        "总在线时长", font_families=['Minecraft AE Pixel'],
        font_size=int(22 * _FS), fill=(80, 80, 80, 255),
    ).to_image()
    window.paste(_time_hdr_img, (CARD_W - 36 - _time_hdr_img.width, rank_hdr_y), alpha=True)

    hdr_div_y = rank_hdr_y + 26
    window.draw_line((36, hdr_div_y, CARD_W - 36, hdr_div_y), fill=(0, 0, 0, 50), width=1)

    playtimes = data.get("playtimes") or data.get("durations") or {}
    list_y0 = HEADER_H + 6
    ROW_BG_EVEN = (198, 198, 198, 255)
    ROW_BG_ODD = (180, 180, 180, 255)
    for i in range(display_count):
        row_y = list_y0 + i * ROW_H
        bg_color = ROW_BG_ODD if i % 2 == 1 else ROW_BG_EVEN
        window.draw_rounded_rectangle(
            (36, row_y + 2, CARD_W - 36, row_y + ROW_H),
            radius=4, fill=bg_color)

        rank_num = str(i + 1)
        if i == 0: rank_color, rank_num = "#FFD700", f"  #{rank_num}"
        elif i == 1: rank_color, rank_num = "#C0C0C0", f"  #{rank_num}"
        elif i == 2: rank_color, rank_num = "#CD7F32", f"  #{rank_num}"
        else: rank_color, rank_num = "white", f"  {rank_num}"

        window.draw_text((60 + 2, row_y + 10 + 2), rank_num,
                         font_families=['Minecraft AE Pixel'], font_size=int(28 * _FS),
                         fill="black", halign="left")
        window.draw_text((60, row_y + 10), rank_num,
                         font_families=['Minecraft AE Pixel'], font_size=int(28 * _FS),
                         fill=rank_color, halign="left")

        if players_hidden and i == 0:
            player_name = "玩家列表已隐藏"
        else:
            player_name = player_names[i] if i < len(player_names) else "——"
        if len(player_name) > 22:
            player_name = player_name[:22] + "..."
        has_player = i < len(player_names) or (players_hidden and i == 0)
        name_color = "white" if has_player else (120, 120, 120, 255)
        window.draw_text((120 + 2, row_y + 10 + 2), player_name,
                         font_families=['Minecraft AE Pixel'], font_size=int(28 * _FS),
                         fill="black", halign="left")
        window.draw_text((120, row_y + 10), player_name,
                         font_families=['Minecraft AE Pixel'], font_size=int(28 * _FS),
                         fill=name_color, halign="left")

        if players_hidden and i == 0:
            time_str = f"{online} 在线"
        else:
            time_str = str(playtimes.get(player_name, "")) if i < len(player_names) and player_name in playtimes else ("-" if i < len(player_names) else "")
        if time_str:
            time_img = Text2Image.from_text(
                time_str, font_families=['Minecraft AE Pixel'],
                font_size=int(28 * _FS), fill=(160, 160, 160, 255),
            ).to_image()
            time_shadow = Text2Image.from_text(
                time_str, font_families=['Minecraft AE Pixel'],
                font_size=int(28 * _FS), fill="black",
            ).to_image()
            time_x = CARD_W - 36 - time_img.width
            window.paste(time_shadow, (time_x + 2, row_y + 12), alpha=True)
            window.paste(time_img, (time_x, row_y + 10), alpha=True)

    footer_y = list_y0 + display_count * ROW_H + 2
    window.draw_line((36, footer_y, CARD_W - 36, footer_y), fill=(0, 0, 0, 80), width=2)

    if server_info.get("status") == "success" and players_hidden:
        status_text = "玩家列表已隐藏，仅显示在线人数"
    elif server_info.get("status") == "success":
        status_text = f"历史玩家: {online}"
    else:
        status_text = "无法连接到服务器"

    window.draw_text((36 + 2, footer_y + 14), status_text,
                     font_families=['Minecraft AE Pixel'], font_size=int(28 * _FS),
                     fill="black", halign="left")
    window.draw_text((36, footer_y + 12), status_text,
                     font_families=['Minecraft AE Pixel'], font_size=int(28 * _FS),
                     fill=(74, 222, 128, 255) if server_info.get("status") == "success" else (248, 113, 113, 255),
                     halign="left")

    img.paste(window, (96, 96), alpha=True)
    output = img.save_png()
    return output

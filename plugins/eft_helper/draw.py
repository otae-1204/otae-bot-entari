from configs.config import Plugin_Config
from configs.path_config import IMAGE_PATH
from plugins.eft_helper.entity import Ammo
from pil_utils import BuildImage
from io import BytesIO

img_path = IMAGE_PATH + "eft_helper/"
config = Plugin_Config("eft_helper")

async def draw_bullet_rough(ammo_data: list[Ammo]) -> dict:
    """
    说明:
        绘制子弹草图 
    参数:
        :param ammo_data: 子弹数据
    返回:
        :return: 图片BytesIO对象
    """
    try:
        # 检测是否有弹药数据
        if len(ammo_data) == 0:
            # 打开无数据图片
            img = BuildImage.open(img_path + "no_data.png")
            
            # 返回图片
            return {"status": True, "data": img.save_png, "type": "Image"}

        # 计算行数
        row_num = 15 if len(ammo_data) > 15 else len(ammo_data)

        # 计算列高 [列高 = 顶内高 + (行数 * 行高) + (行数 - 1) * 行间隔 + 底内高]
        col_height = 150 + (row_num * 425) + (row_num - 1) * 65 + 100

        # 计算列宽 [列宽= 左内宽 + 行长 + 右内宽]
        col_width = 80 + 1530 + 80

        # 计算列数
        col_num = int(
            len(ammo_data) / 15) if len(ammo_data) % 15 == 0 else int(len(ammo_data) / 15) + 1
        
        # 计算背景高度 [背景高度 = 顶外高 + 列高 + 底外高]
        bg_height = 600 + col_height + 150

        # 计算背景宽度 [背景宽度 = 左外宽 + (列宽 * 列数) + (列数 - 1) * 列间隔 + 右外宽]
        bg_width = 150 + (col_width * col_num) + (col_num - 1) * 125 + 150

        # 创建背景
        bg = BuildImage.new("RGBA", (bg_width, bg_height), (0, 0, 0, 0))

        # 拼接背景
        bg_img = BuildImage.open(img_path + "/UI/bg.png")
        for i in range(0, bg_width, 792):
            for j in range(0, bg_height, 792):
                bg.paste(bg_img, (i, j))

        # 粘贴标题
        title_img = BuildImage.open(img_path + "/UI/title_rough.png")
        bg.paste(title_img, (100, 150), alpha=True)

        # 分割数据,15一分
        ammo_data_list = [ammo_data[i:i + 15] for i in range(0, len(ammo_data), 15)]

        # 绘制列
        for col in range(0, col_num):
            # 获取行数
            col_row_num = len(ammo_data_list[col])

            # 获取列高
            col_h = 150 + (col_row_num * 425) + (col_row_num - 1) * 65 + 100

            # 创建列图片
            col_img = BuildImage.new("RGBA", (col_width, col_h), (0, 0, 0, 0))
            # 画出列背景
            col_img = col_img.draw_rounded_rectangle(
                (0, 0, col_width, col_h), radius=50, fill=(161, 198, 234, 255))
            # 画出内部背景
            col_img = col_img.draw_rounded_rectangle(
                (10, 10, col_width-10,col_h-10), radius=50, fill=(218, 227, 229, 255))

            # 绘制行
            for row in range(0, col_row_num):
                # 获取弹药数据
                ammo = ammo_data_list[col][row]

                # 绘制弹药图片
                ammo_img = BuildImage.open(ammo.img)
                ammo_img = ammo_img.resize((400, 400), keep_ratio=True)
                col_img.paste(ammo_img, (80, 150 + row * 490), alpha=True)


            # 粘贴列
            bg.paste(col_img, (150 + col * (col_width + 125), 600), alpha=True)
        
        # 保存图片
        img = bg.save_png()
        return {"status": True, "data": img, "type": "Image"}


    except Exception as e:
        return {"status": False, "data": str(e), "type": "Error"}

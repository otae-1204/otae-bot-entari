import cv2
from PIL import Image
from typing import Tuple
import numpy as np

from .pillow import PILBuildImage

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

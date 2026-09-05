from pathlib import Path
from typing import Optional

# 图片路径
IMAGE_PATH = Path("assets/image/")
# 音频路径
AUDIO_PATH = Path("assets/audio/")
# 文本路径
TEXT_PATH = Path("assets/txt/")
# 字体路径
FONT_PATH = Path("assets/font/")
# 临时图片路径
TEMP_PATH = Path("assets/image/temp/")
# Json路径
JSON_PATH = Path("assets/json/")
# SQL路径
SQL_PATH = Path("assets/sql/")


def init_path():
    global IMAGE_PATH, AUDIO_PATH, TEXT_PATH, FONT_PATH, TEMP_PATH, JSON_PATH, SQL_PATH
    IMAGE_PATH.mkdir(parents=True, exist_ok=True)
    AUDIO_PATH.mkdir(parents=True, exist_ok=True)
    TEXT_PATH.mkdir(parents=True, exist_ok=True)
    FONT_PATH.mkdir(parents=True, exist_ok=True)
    TEMP_PATH.mkdir(parents=True, exist_ok=True)
    JSON_PATH.mkdir(parents=True, exist_ok=True)
    SQL_PATH.mkdir(parents=True, exist_ok=True)

    IMAGE_PATH = str(IMAGE_PATH.absolute()) + '/'
    AUDIO_PATH = str(AUDIO_PATH.absolute()) + '/'
    TEXT_PATH = str(TEXT_PATH.absolute()) + '/'
    FONT_PATH = str(FONT_PATH.absolute()) + '/'
    TEMP_PATH = str(TEMP_PATH.absolute()) + '/'
    JSON_PATH = str(JSON_PATH.absolute()) + '/'
    SQL_PATH = str(SQL_PATH.absolute()) + '/'


init_path()

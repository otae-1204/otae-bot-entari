from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Union

from arclet.entari import At, Audio, Image, Text

from configs.path_config import AUDIO_PATH, IMAGE_PATH


def image(
    img_name: Union[str, Path] = None,
    path: str = None,
    abspath: str = None,
    b64: str = None,
):
    if abspath:
        img_path = Path(abspath)
        return Image.of(path=img_path) if img_path.exists() else ""
    if isinstance(img_name, Path):
        if img_name.exists():
            return Image.of(path=img_name)
        logging.error("image %s missing", img_name.absolute())
        return ""
    if b64:
        raw = b64.split("base64://", 1)[-1]
        return Image.of(raw=base64.b64decode(raw))
    if not img_name:
        return ""
    if "http" in img_name:
        return Image(src=img_name)
    if len(img_name.split(".")) == 1:
        img_name += ".jpg"
    file = Path(IMAGE_PATH) / path / img_name if path else Path(IMAGE_PATH) / img_name
    if file.exists():
        return Image.of(path=file)
    logging.error("image %s missing", file.absolute())
    return ""


def at(qq: int):
    return At(str(qq))


def record(voice_name: str, path: str = None):
    if len(voice_name.split(".")) == 1:
        voice_name += ".mp3"
    file = Path(AUDIO_PATH) / path / voice_name if path else Path(AUDIO_PATH) / voice_name
    if "http" in voice_name:
        return Audio(src=voice_name)
    if file.exists():
        return Audio.of(path=file)
    logging.error("audio %s missing", file.absolute())
    return ""


def text(msg: str):
    return Text(str(msg))

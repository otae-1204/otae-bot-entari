"""Small, sanitized site-icon payloads shared by the fetcher and offline renderer."""
from contextlib import ExitStack, closing
from io import BytesIO
import warnings

from PIL import Image, ImageOps

MAX_ICONS = 32
ICON_EDGE = 64
MAX_ICON_BYTES = 24 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024


def normalize_icon(raw: bytes) -> bytes:
    with warnings.catch_warnings(), ExitStack() as resources:
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        source = resources.enter_context(closing(Image.open(resources.enter_context(BytesIO(raw)))))
        if (source.format not in ('ICO', 'PNG', 'JPEG', 'WEBP', 'GIF')
                or min(source.size) < 1 or max(source.size) > 1024):
            raise ValueError('unsupported_icon')
        source.seek(0)
        rgba = resources.enter_context(closing(source.convert('RGBA')))
        resized = resources.enter_context(closing(ImageOps.contain(
            rgba, (ICON_EDGE, ICON_EDGE), Image.Resampling.LANCZOS)))
        canvas = resources.enter_context(closing(Image.new('RGBA', (ICON_EDGE, ICON_EDGE))))
        canvas.paste(resized, ((ICON_EDGE - resized.width) // 2, (ICON_EDGE - resized.height) // 2))
        output = resources.enter_context(BytesIO())
        canvas.save(output, 'PNG', compress_level=3)
        result = output.getvalue()
        validate_icon(result)
        return result


def validate_icon(raw: bytes):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ICON_BYTES:
        raise ValueError('invalid_icon_size')
    with BytesIO(raw) as source, Image.open(source) as image:
        if image.format != 'PNG' or image.size != (ICON_EDGE, ICON_EDGE) or image.mode != 'RGBA':
            raise ValueError('invalid_icon_format')
        image.verify()

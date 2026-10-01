"""Offline resident Pillow worker; parent owns deadlines and process-group cleanup."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import base64
import io
import json
import os
from pathlib import Path
import sys
import time

from .rendering import MAX_CARD_HEIGHT, MAX_IMAGE_BYTES, RenderError, RENDER_LIMIT_CODES, engine_error_code


class RenderRuntime:
    """Only font assets and the parser survive requests. Never cache answer/layout/results."""
    def __init__(self, font_set=None):
        from md2png.hyw.font_assets import HywFontAssets
        from .render_protocol import AnswerParser
        self.font_set = font_set
        self.font_assets = HywFontAssets(font_set=font_set)
        self.parser = AnswerParser()

    def assets(self, font_set=None):
        from md2png.hyw.font_assets import HywFontAssets
        if font_set != self.font_set or not self.font_assets.unchanged():
            self.font_assets = HywFontAssets(font_set=font_set)
            self.font_set = font_set
        return self.font_assets

    def warm(self):
        from md2png.hyw import render_document
        from md2png.model import Limits
        from .pillow_card import adapt_answer
        # Synthetic, public input exercises imports, cmap/font bytes, mathtext and codecs.
        # It is not a cached answer and never appears in user diagnostics or output.
        sample = '# 准备 Ready\n\n<summary>中文字体 warm</summary>\n\n**粗体** *Italic* `code` [链接](https://example.com) $x^2$\n\n```python\nx = 1\n```'
        parsed = self.parser.parse(sample)
        document = adapt_answer(parsed, {}, Limits(), reading=True)
        result = render_document(document, font_assets=self.assets(self.font_set), reject_overflow=True)
        with closing(result.image), io.BytesIO() as output:
            result.image.save(output, format='PNG', compress_level=3)

    def close(self):
        self.parser.close()


def render(work: Path, runtime: RenderRuntime | None = None) -> dict:
    from md2png.fonts import FontSet
    from md2png.hyw import render_document
    from md2png.model import Limits, RenderError as EngineError
    from .pillow_card import adapt_answer, parse_protocol, referenced_assets

    started = time.perf_counter()
    timings = {}
    data = json.loads((work / 'input.json').read_text(encoding='utf-8'))
    answer = data['answer']
    if not isinstance(answer, str) or len(answer.encode('utf-8')) > 256 * 1024:
        raise RenderError('render_text_limit')
    stage = time.perf_counter()
    parsed = runtime.parser.parse(answer) if runtime else parse_protocol(answer)
    timings['protocol_ms'] = round((time.perf_counter() - stage) * 1000, 2)
    limits = Limits(max_chars=256 * 1024, max_height=MAX_CARD_HEIGHT, timeout=40)
    try:
        stage = time.perf_counter()
        from .media import MAX_IMAGES, MAX_JPEG_BYTES, MAX_EDGE
        from PIL import Image
        encoded_assets = data.get('image_assets', {})
        max_tool_images = data.get('max_tool_images', MAX_IMAGES)
        if (type(max_tool_images) is not int or max_tool_images < 0
                or not isinstance(encoded_assets, dict) or len(encoded_assets) > max_tool_images):
            raise RenderError('render_asset_invalid')
        encoded_icons = data.get('favicon_assets', {})
        if not isinstance(encoded_icons, dict):
            raise RenderError('render_asset_invalid')
        document = adapt_answer(parsed, data.get('metadata') or {}, limits, reading=True,
                                image_urls=set(encoded_assets))
        encoded_assets, encoded_icons = referenced_assets(document, encoded_assets, encoded_icons)
        image_assets = {}
        for url, encoded in encoded_assets.items():
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_JPEG_BYTES + 2) // 3):
                raise RenderError('render_asset_invalid')
            raw = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != 'JPEG' or max(image.size) > MAX_EDGE:
                    raise RenderError('render_failed')
                image.verify()
            image_assets[url] = raw
        from .favicon_assets import MAX_ICONS, MAX_ICON_BYTES, validate_icon
        from md2png.hyw.document import source_origin
        if len(encoded_icons) > MAX_ICONS:
            raise RenderError('render_asset_invalid')
        source_origins = {source_origin(ref.url) for ref in document.references}
        for origin, encoded in encoded_icons.items():
            if (not isinstance(origin, str) or not origin or source_origin(origin) != origin
                    or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ICON_BYTES + 2) // 3)):
                raise RenderError('render_asset_invalid')
            raw = base64.b64decode(encoded, validate=True)
            validate_icon(raw)
            if origin in source_origins:
                image_assets['favicon:' + origin] = raw
        timings['adapt_ms'] = round((time.perf_counter() - stage) * 1000, 2)
        font_set = FontSet.from_dict(data['font_set']) if data.get('font_set') else None
        stage = time.perf_counter()
        assets = runtime.assets(font_set) if runtime else None
        timings['assets_ms'] = round((time.perf_counter() - stage) * 1000, 2)
        result = render_document(document, limits=limits, scale=1, reject_overflow=True,
                                 font_assets=assets, font_set=font_set if assets is None else None, assets=image_assets, timings=timings)
    except EngineError as error:
        raise RenderError(engine_error_code(error)) from error
    output = work / 'card.png'
    stage = time.perf_counter()
    with closing(result.image):
        result.image.save(output, format='PNG', compress_level=3)
        # Retry the original compression level before enforcing the byte budget.
        if output.stat().st_size > MAX_IMAGE_BYTES:
            result.image.save(output, format='PNG', compress_level=6)
        output.chmod(0o600)
        if output.stat().st_size > MAX_IMAGE_BYTES:
            raise RenderError('render_file_limit')
    timings['encode_ms'] = round((time.perf_counter() - stage) * 1000, 2)
    timings['worker_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return {'ok': True, 'recovered': parsed['recovered'], 'mode': parsed['mode'],
            'diagnostics': [asdict(d) for d in result.scene.diagnostics], 'timings': timings,
            'links': [reference.url for reference in document.references]}


def safe_render(work: Path, runtime: RenderRuntime | None = None) -> dict:
    try:
        return render(work, runtime)
    except RenderError as error:
        return {'ok': False, 'code': error.code}
    except Exception:
        return {'ok': False, 'code': 'render_failed'}


def emit(result: dict):
    raw = json.dumps(result, ensure_ascii=False).encode() + b'\n'
    if len(raw) > 2 * 1024 * 1024:
        raw = b'{"ok":false,"code":"render_protocol_limit"}\n'
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def peak_rss_mb() -> float:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('page_faults', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('peak_working_set', 'working_set',
                'peak_paged_pool', 'paged_pool', 'peak_nonpaged_pool', 'nonpaged_pool',
                'pagefile', 'peak_pagefile')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        query = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        if not query(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.peak_working_set / (1024 * 1024)
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == 'darwin' else 1024)


def serve_worker():
    runtime = None
    try:
        from md2png.fonts import FontSet
        config = json.loads(os.environ.pop('HYW_RENDER_FONTS', 'null'))
        runtime = RenderRuntime(FontSet.from_dict(config) if config else None)
        runtime.warm()
        emit({'type': 'ready', 'ok': True})
        while True:
            raw = sys.stdin.buffer.readline(4097)
            if not raw:
                return 0
            if len(raw) > 4096 or not raw.endswith(b'\n'):
                return 1
            command = json.loads(raw)
            result = safe_render(Path(command['work']), runtime)
            result['request_complete'] = True
            # High-water is a conservative recycling trigger, not a hard allocation cap.
            result['rss_mb'] = peak_rss_mb()
            emit(result)
            # A fully unwound input/size rejection leaves no request state in assets.
            # Runtime/parser failures still retire the worker and its entire group.
            if not result['ok'] and result.get('code') not in RENDER_LIMIT_CODES:
                return 1
    except Exception:
        emit({'ok': False, 'code': 'render_failed'})
        return 1
    finally:
        if runtime:
            runtime.close()


def main():
    if sys.argv[1:] == ['--serve']:
        return serve_worker()
    work = Path(sys.argv[1])
    result = safe_render(work)
    output = work / 'result.json'
    output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    output.chmod(0o600)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

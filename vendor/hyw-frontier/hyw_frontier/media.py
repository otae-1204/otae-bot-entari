"""Bounded search/Reader image previews: task-owned memory, no compression service.

The fetch subprocess has a hard lifetime (including DNS/redirects). Pillow compression
and model payload construction remain in memory; only approved bytes reach rendering.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
from html import unescape
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Event, Lock
import time
import warnings
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageOps

from .jina import JinaError, public_url
from .media_fetch import MAX_BYTES as MAX_DOWNLOAD_BYTES
from .media_refs import display_url, is_display_url
from .prompt_files import read_prompt

MAX_IMAGES = 600
MAX_READER_IMAGES = 30
MAX_IMAGES_PER_ROUND = None  # No per-round count cap; the task budget still applies.
DOWNLOAD_TIMEOUT = 2.5
REVERSE_IMAGE_DOWNLOAD_TIMEOUT = DOWNLOAD_TIMEOUT * 2
DOWNLOAD_CONCURRENCY = 20
READER_PRIORITY_IMAGES = 3
MAX_JPEG_BYTES = 256 * 1024
MAX_EDGE = 1280
MAX_PIXELS = 25_000_000
MEDIA_CONFIG = {'enabled': True, 'max_images': MAX_IMAGES, 'max_images_per_round': MAX_IMAGES_PER_ROUND,
                'max_reader_images': MAX_READER_IMAGES,
                'download_timeout_seconds': DOWNLOAD_TIMEOUT, 'max_download_bytes': MAX_DOWNLOAD_BYTES,
                'reverse_image_download_timeout_seconds': REVERSE_IMAGE_DOWNLOAD_TIMEOUT,
                'concurrency': DOWNLOAD_CONCURRENCY, 'format': 'image/jpeg', 'max_edge': MAX_EDGE,
                'jpeg_quality': 75, 'max_image_bytes': MAX_JPEG_BYTES,
                'discovery': 'search_images_reverse_image_matches_and_reader_image_links', 'compression': 'in_memory',
                'reader_priority_images': READER_PRIORITY_IMAGES, 'unused_slots': 'shared',
                'rendering': 'approved_bytes_only', 'attachment_binding': 'adjacent_id_display_url_label',
                'display_url_scheme': 'hyw-media', 'budget_includes_pageshots': True}
_URL = re.compile(r'''https?://[^\s<>"'`\[\]，。；！？、（）【】]+''')
_MARKDOWN_IMAGE = re.compile(r'!\[([^\]\n]*)\]\(<?(https?://[^\s)>]+)>?(?:\s+"[^"]*")?\)')
_ORIGINAL_LINK = re.compile(r'(?<!!)\[([^\]\n]*(?:查看原图|原图|Original file|下载|download)[^\]\n]*)\]\(<?(https?://[^\s)>]+)>?(?:\s+"[^"]*")?\)', re.I)
_IMAGE_PATH = re.compile(r'\.(?:jpe?g|png|webp|gif)(?:$|/)', re.I)


@dataclass(frozen=True)
class Candidate:
    url: str
    source_url: str
    title: str
    owner: int
    engines: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    reader_page: str = ""


def discover(text: str, source_url: str, title: str, owner: int):
    """Use actual image links only; never invent URLs or fetch article HTML for metadata."""
    seen = set()
    links = [(m[2], m[1] or title) for m in _ORIGINAL_LINK.finditer(text)]
    links.extend((m[2], m[1] or title) for m in _MARKDOWN_IMAGE.finditer(text))
    for match in _URL.finditer(text):
        value = unescape(match[0]).rstrip('.,;:!?)')
        try:
            parts = urlsplit(value)
            if _IMAGE_PATH.search(parts.path):
                links.append((value, title))
            # Sharing buttons sometimes expose the real image only as an encoded parameter.
            query = parse_qs(parts.query, max_num_fields=100)
            for key in ('media', 'image', 'image_url', 'imgurl'):
                for image in query.get(key, []):
                    links.append((image, title))
        except ValueError:
            continue
    for value, label in links:
        try:
            url = public_url(unescape(value))
        except JinaError:
            continue
        if len(url) > 2000 or url in seen:
            continue
        seen.add(url)
        yield Candidate(url, source_url, label[:200], owner)


def download(candidate: Candidate, cancel: Event, timeout: float = DOWNLOAD_TIMEOUT):
    started = time.monotonic()
    raw, status = None, 'download_failed'
    if cancel.is_set():
        return candidate, raw, 'cancelled', 0
    try:
        result = subprocess.run(
            [sys.executable, '-I', str(Path(__file__).with_name('media_fetch.py')), str(timeout)],
            input=candidate.url.encode(), capture_output=True, timeout=timeout,
            env={k: os.environ[k] for k in ('SYSTEMROOT', 'WINDIR') if k in os.environ},
        )
        if result.returncode == 0 and result.stdout:
            raw, status = result.stdout, 'downloaded'
        elif re.fullmatch(rb'http_[1-5][0-9]{2}', result.stderr):
            status = 'download_' + result.stderr.decode('ascii')
    except subprocess.TimeoutExpired:
        status = 'download_timeout'
    except OSError:
        pass
    return candidate, raw, status, round((time.monotonic() - started) * 1000, 2)


def compress(raw: bytes, *, min_edge: int = 64):
    """One fast resize/JPEG pass; a second smaller pass only for unusually noisy images."""
    with warnings.catch_warnings(), ExitStack() as resources:
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        source = resources.enter_context(closing(Image.open(resources.enter_context(BytesIO(raw)))))
        if (source.format not in ('JPEG', 'PNG', 'WEBP', 'GIF')
                or source.width * source.height > MAX_PIXELS
                or min(source.size) < min_edge):
            raise ValueError('unsupported_image')
        source.seek(0)  # Animated images use the first frame only.
        source.draft('RGB', (MAX_EDGE, MAX_EDGE))
        image = resources.enter_context(closing(ImageOps.exif_transpose(source)))
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.BILINEAR, reducing_gap=2.0)
        if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
            rgba = resources.enter_context(closing(image.convert('RGBA')))
            image = resources.enter_context(closing(Image.new('RGB', rgba.size, 'white')))
            mask = resources.enter_context(closing(rgba.getchannel('A')))
            image.paste(rgba, mask=mask)
        else:
            image = resources.enter_context(closing(image.convert('RGB')))
        buffer = resources.enter_context(BytesIO())
        image.save(buffer, 'JPEG', quality=75, subsampling=2, optimize=False, progressive=False)
        if buffer.tell() > MAX_JPEG_BYTES:
            image.thumbnail((960, 960), Image.Resampling.BILINEAR)
            buffer.close()
            buffer = resources.enter_context(BytesIO())
            image.save(buffer, 'JPEG', quality=65, subsampling=2, optimize=False, progressive=False)
        if buffer.tell() > MAX_JPEG_BYTES:
            raise ValueError('compressed_image_too_large')
        return buffer.getvalue(), image.size


class ImagePipeline:
    def __init__(self, history=(), *, max_images: int = MAX_IMAGES,
                 max_reader_images: int = MAX_READER_IMAGES):
        if type(max_images) is not int or max_images < 0:
            raise ValueError('max_tool_images must be a non-negative integer')
        self.max_images = max_images
        if type(max_reader_images) is not int or max_reader_images < 0:
            raise ValueError('max_reader_images must be a non-negative integer')
        self.max_reader_images = max_reader_images
        self.reader_attempts: dict[str, int] = {}
        self.assets: dict[str, bytes] = {}
        self.seen: set[str] = set()
        self._budget_lock = Lock()
        # Reuse previews within the request-selected tool budget. User images and
        # parsed chat records are separate; history replay never downloads again.
        for message in history:
            if message.get('role') != 'toolResult' or message.get('toolName') == 'crop_user_image':
                continue
            content = message.get('content', [])
            try:
                data = json.loads(content[0]['text'])
                rows = data.get('media_images', [])
                # Older pageshot crops predate the shared media_images contract.
                if not rows and message.get('toolName') == 'jina_pageshot' and data.get('display_url'):
                    rows = [{**data, 'status': 'ready'}]
                if message.get('toolName') == 'jina_read_url':
                    for row in rows:
                        page = row.get('source_url', '')
                        self.reader_attempts[page] = self.reader_attempts.get(page, 0) + 1
                blocks = [b for b in content if b.get('type') == 'image']
                for row, block in zip((r for r in rows if r.get('status') == 'ready'), blocks):
                    url = row.get('display_url') or public_url(row['url'])
                    if not is_display_url(url):
                        public_url(url)  # Validate legacy URLs without losing crop fragments.
                    data = block.get('data', '')
                    if (len(self.assets) < self.max_images and block.get('mimeType') == 'image/jpeg'
                            and len(data) <= 4 * ((MAX_JPEG_BYTES + 2) // 3)):
                        raw = base64.b64decode(data, validate=True)
                        if len(raw) > MAX_JPEG_BYTES:
                            continue
                        self.assets[url] = raw
                        # Discovery still deduplicates by original URL, not display reference.
                        self.seen.add(public_url(row['url']) if row.get('url') else url)
            except (ValueError, KeyError, TypeError, IndexError, JinaError):
                continue
        history_count = sum(b.get('type') == 'image' for m in history if m.get('role') == 'toolResult'
                            and m.get('toolName') != 'crop_user_image' for b in m.get('content', []))
        for index in range(max(0, history_count - len(self.seen))):
            self.seen.add(f'history-image:{index}')
        self.prepared = history_count

    def reserve_tool_image(self) -> bool:
        """Reserve a screenshot/crop attempt before parallel tool workers do work."""
        with self._budget_lock:
            if len(self.seen) >= self.max_images:
                return False
            self.seen.add(f'tool-image-attempt:{len(self.seen)}')
            return True

    def close(self):
        self.assets.clear()
        self.seen.clear()
        self.reader_attempts.clear()
        self.prepared = 0

    def prepare(self, results: list[dict], cancel: Event, emit):
        if len(self.seen) >= self.max_images or cancel.is_set():
            return {'download_ms': 0, 'processing_ms': 0, 'new_images': 0}
        # Bound discovery by remaining task budget, not by download concurrency.
        remaining = self.max_images - len(self.seen)
        page_counts = self.reader_attempts.copy()
        pools = {True: [], False: []}
        pooled_urls = {True: set(), False: set()}
        for owner, result in enumerate(results):
            if result.get('toolName') not in ('web_search', 'search_images', 'jina_read_url', 'reverse_image_search') or result.get('isError'):
                continue
            reader = result['toolName'] in ('jina_read_url', 'reverse_image_search')
            pool, urls = pools[reader], pooled_urls[reader]
            if len(pool) >= remaining:
                continue
            data = json.loads(result['content'][0]['text'])
            for batch in data.get('results', []):
                if not batch.get('ok'):
                    continue
                rows = batch.get('results', []) if result['toolName'] in ('web_search', 'search_images') else [batch]
                if result['toolName'] == 'reverse_image_search':
                    rows = batch.get('matches', [])
                for row in rows:
                    url, title = row.get('url', ''), row.get('title', '')
                    text = row.get('snippet', row.get('content', '')) + '\n' + url
                    if result['toolName'] in ('search_images', 'reverse_image_search'):
                        try:
                            discovered = [Candidate(public_url(row.get('image_url', '')), public_url(url), title, owner,
                                                    engines=tuple(row.get('engines', [])),
                                                    source_urls=tuple(dict.fromkeys(source['url'] for source in row.get('sources', []))))]
                        except JinaError:
                            continue
                    else:
                        discovered = discover(text, url, title, owner)
                    for candidate in discovered:
                        if len(pool) >= remaining:
                            break
                        if candidate.url in self.seen or candidate.url in urls:
                            continue
                        if result['toolName'] == 'jina_read_url':
                            if page_counts.get(url, 0) >= self.max_reader_images:
                                break
                            page_counts[url] = page_counts.get(url, 0) + 1
                            candidate = replace(candidate, reader_page=url)
                        urls.add(candidate.url)
                        pool.append(candidate)
        # Reader gets the first three distinct slots; other tools get the rest.
        # Both pools share the remaining task budget, without a per-round cap.
        reader_images, other_images = pools[True], pools[False]
        ordered = (reader_images[:READER_PRIORITY_IMAGES] + other_images
                   + reader_images[READER_PRIORITY_IMAGES:])
        candidates = []
        for candidate in ordered:
            if len(self.seen) >= self.max_images:
                break
            if candidate.url not in self.seen:
                self.seen.add(candidate.url)
                if candidate.reader_page:
                    page = candidate.reader_page
                    self.reader_attempts[page] = self.reader_attempts.get(page, 0) + 1
                candidates.append(candidate)
        if not candidates or cancel.is_set():
            return {'download_ms': 0, 'processing_ms': 0, 'new_images': 0}
        # Attachment order follows tool-result order, independent of quota priority.
        candidates.sort(key=lambda candidate: candidate.owner)
        emit({'type': 'media_start', 'candidates': len(candidates), 'attempted': len(self.seen),
              'limit': self.max_images, 'round_limit': MAX_IMAGES_PER_ROUND})
        by_owner: dict[int, list[dict]] = {}
        image_blocks: dict[int, list[dict]] = {}
        details = []
        new_images = 0
        download_ms = processing_ms = 0.0
        # Release each batch's original bytes before fetching more images. A page
        # with hundreds of images must not retain every full-size download at once.
        with ThreadPoolExecutor(max_workers=min(DOWNLOAD_CONCURRENCY, len(candidates))) as pool:
            for offset in range(0, len(candidates), DOWNLOAD_CONCURRENCY):
                if cancel.is_set():
                    break
                started = time.monotonic()
                downloaded = list(pool.map(lambda c: download(
                    c, cancel, REVERSE_IMAGE_DOWNLOAD_TIMEOUT
                    if results[c.owner]['toolName'] == 'reverse_image_search' else DOWNLOAD_TIMEOUT),
                    candidates[offset:offset + DOWNLOAD_CONCURRENCY]))
                download_ms += (time.monotonic() - started) * 1000
                processing = time.monotonic()
                for candidate, raw, status, elapsed in downloaded:
                    row = {'url': candidate.url, 'source_url': candidate.source_url, 'title': candidate.title,
                           'status': status, 'download_ms': elapsed}
                    if candidate.engines:
                        row.update(engines=list(candidate.engines), source_urls=list(candidate.source_urls))
                    before = time.monotonic()
                    if raw is not None and not cancel.is_set():
                        try:
                            jpeg, (width, height) = compress(raw)
                            reference = display_url(candidate.url, jpeg)
                            self.assets[reference] = jpeg
                            new_images += 1
                            self.prepared += 1
                            row.update(status='ready', image_id=f'image_{self.prepared}', display_url=reference,
                                       attachment_order=new_images, width=width, height=height,
                                       orientation='landscape' if width > height else 'portrait' if height > width else 'square',
                                       bytes=len(jpeg), mimeType='image/jpeg')
                            image_blocks.setdefault(candidate.owner, []).extend([
                                {'type': 'text', 'text': json.dumps({
                                    'media_attachment': row['image_id'], 'image_id': row['image_id'],
                                    'display_url': reference, 'url': candidate.url,
                                    'source_url': candidate.source_url, 'width': width, 'height': height,
                                    **({'engines': list(candidate.engines), 'source_urls': list(candidate.source_urls)}
                                       if candidate.engines else {})},
                                    ensure_ascii=False)},
                                {'type': 'image', 'mimeType': 'image/jpeg', 'data': base64.b64encode(jpeg).decode('ascii')},
                            ])
                        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                            row['status'] = 'compression_rejected'
                    row['processing_ms'] = round((time.monotonic() - before) * 1000, 2)
                    by_owner.setdefault(candidate.owner, []).append(row)
                    details.append(row)
                raw = None
                downloaded.clear()
                processing_ms += (time.monotonic() - processing) * 1000
        download_ms = round(download_ms, 2)
        emit({'type': 'media_download_end', 'duration_ms': download_ms, 'candidates': len(details)})
        processing = time.monotonic()
        for owner, rows in by_owner.items():
            result = results[owner]
            data = json.loads(result['content'][0]['text'])
            data['media_images'] = rows
            data['media_notice'] = read_prompt('media_notice.md')
            result['content'][0]['text'] = json.dumps(data, ensure_ascii=False)
            result['content'].extend(image_blocks.get(owner, []))
        processing_ms = round(processing_ms + (time.monotonic() - processing) * 1000, 2)
        emit({'type': 'media_end', 'download_ms': download_ms, 'processing_ms': processing_ms,
              'new_images': new_images, 'prepared': self.prepared, 'attempted': len(self.seen),
              'limit': self.max_images, 'round_limit': MAX_IMAGES_PER_ROUND, 'images': details})
        return {'download_ms': download_ms, 'processing_ms': processing_ms, 'new_images': new_images}

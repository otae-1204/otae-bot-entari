"""Stage timing and bounded, image-free records shared by local and bot logs."""
from contextlib import contextmanager
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

_INLINE = re.compile(r'data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]+')
_URL = re.compile(r'https?://[^\s<>"\']+')
_SECRET_KEYS = {'authorization', 'api_key', 'apikey', 'password', 'access_token', 'refresh_token',
                'thoughtsignature', 'signature', 'thinkingsignature', 'providermetadata', 'providerdetails',
                'thinking', 'rawthinking', 'reasoning_content', 'image_assets', 'favicon_assets'}


def _url(value):
    try:
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, parts.hostname or '', parts.path, '', ''))
    except ValueError:
        return '[invalid URL]'


def redact(value, depth=0):
    if depth > 16:
        return '[depth limit]'
    if isinstance(value, dict):
        if value.get('type') == 'image':
            return {'type': 'image', 'mimeType': value.get('mimeType'),
                    'base64_chars_omitted': len(value.get('data', '')) or value.get('base64_length', value.get('base64_chars_omitted', 0))}
        if str(value.get('type', '')).startswith('thinking'):
            return {**{k: value[k] for k in ('type', 'round', 'index', 'elapsed_ms') if k in value},
                    'chars_omitted': len(value.get('thinking', value.get('content', value.get('delta', ''))))
                    or value.get('chars_omitted', 0)}
        return {str(k): redact(v, depth + 1) for k, v in list(value.items())[:300]
                if str(k).lower() not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [redact(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        if value.startswith(('{', '[')):
            try:
                decoded = json.loads(value)
            except ValueError:
                pass
            else:
                return json.dumps(redact(decoded, depth + 1), ensure_ascii=False)
        text = _URL.sub(lambda m: _url(m[0]), _INLINE.sub('[inline image omitted]', value))
        return text if len(text) <= 64000 else text[:64000] + '[text truncated]'
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return {'bytes_omitted': len(value)}
    return f'[{type(value).__name__} omitted]'


@contextmanager
def stage(emit, name, **identity):
    """Wall time of one operation; nested/concurrent stages must not be added."""
    started = time.monotonic()
    info = {'stage': name, 'stage_id': uuid.uuid4().hex[:16], **identity}
    metrics = {}
    def notify(event):
        if emit is not None:
            try:
                emit(event)
            except Exception:
                pass  # Observability must not interrupt the work being measured.
    notify({'type': 'stage_start', **info})
    try:
        yield metrics
    except BaseException as exc:
        metrics.update(status='cancelled' if type(exc).__name__ == 'CancelledError' else 'error',
                       error_type=type(exc).__name__)
        raise
    finally:
        metrics.setdefault('status', 'ok')
        notify({'type': 'stage_end', **info, **metrics,
                'duration_ms': round((time.monotonic() - started) * 1000, 2)})

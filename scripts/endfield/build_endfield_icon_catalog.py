#!/usr/bin/env python3
"""Build a searchable HTML catalog and CSV inventory for exported UI PNGs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import struct
from pathlib import Path
from urllib.parse import quote


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(f"Not a supported PNG: {path}")
    return struct.unpack(">II", header[16:24])


def infer_category(relative_path: str) -> str:
    parts = relative_path.split("/")
    lowered = [part.lower() for part in parts]
    if "sprites" in lowered:
        index = lowered.index("sprites")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-2] if len(parts) > 1 else "root"


def collect_images(input_root: Path, output_file: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(input_root.rglob("*.png"), key=lambda item: item.as_posix().lower()):
        width, height = read_png_size(path)
        relative_path = path.relative_to(input_root).as_posix()
        browser_path = Path(os.path.relpath(path, output_file.parent)).as_posix()
        rows.append(
            {
                "name": path.name,
                "path": relative_path,
                "url": quote(browser_path, safe="/"),
                "category": infer_category(relative_path),
                "width": width,
                "height": height,
                "bytes": path.stat().st_size,
            }
        )
    return rows


def write_csv(rows: list[dict[str, object]], output_file: Path) -> Path:
    csv_file = output_file.with_suffix(".csv")
    with csv_file.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("name", "path", "category", "width", "height", "bytes"),
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
    return csv_file


def write_html(rows: list[dict[str, object]], output_file: Path) -> None:
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    output_file.write_text(
        """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Endfield UI Icon Catalog</title>
<style>
:root{color-scheme:dark;font-family:Inter,"Microsoft YaHei",system-ui,sans-serif;background:#101211;color:#eef3ef}
*{box-sizing:border-box}body{margin:0}header{position:sticky;top:0;z-index:2;padding:18px 24px 14px;background:rgba(16,18,17,.94);backdrop-filter:blur(16px);border-bottom:1px solid #303632}
h1{font-size:20px;margin:0 0 12px;letter-spacing:.03em}.tools{display:grid;grid-template-columns:minmax(240px,1fr) 220px auto;gap:10px}
input,select,button{border:1px solid #3b443e;background:#1a1f1c;color:#f5f7f5;border-radius:3px;padding:10px 12px;font:inherit}button{cursor:pointer;background:#b9ff42;color:#111;font-weight:700;border-color:#b9ff42}
.meta{color:#9aa59e;font-size:12px;margin-top:10px}main{padding:18px 24px 32px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(172px,1fr));gap:10px}
.card{min-width:0;border:1px solid #2d342f;background:#171b18;border-radius:4px;overflow:hidden}.preview{height:148px;display:grid;place-items:center;background-color:#252a27;background-image:linear-gradient(45deg,#303632 25%,transparent 25%),linear-gradient(-45deg,#303632 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#303632 75%),linear-gradient(-45deg,transparent 75%,#303632 75%);background-size:20px 20px;background-position:0 0,0 10px,10px -10px,-10px 0}
.preview img{display:block;max-width:138px;max-height:124px;image-rendering:auto}.info{padding:9px}.name{font-size:12px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.path{font-size:10px;color:#87928b;height:30px;overflow:hidden;margin-top:5px;word-break:break-all}.dim{font-size:10px;color:#b9ff42;margin-top:6px}.empty{text-align:center;color:#87928b;padding:60px}.more{display:block;margin:20px auto 0;min-width:180px}
@media(max-width:720px){header,main{padding-left:12px;padding-right:12px}.tools{grid-template-columns:1fr 1fr}.tools button{grid-column:1/-1}}
</style>
</head>
<body>
<header><h1>ENDFIELD / UI ICON CATALOG</h1><div class="tools"><input id="query" placeholder="搜索名称或完整路径…"><select id="category"><option value="">全部分类</option></select><button id="reset">清除筛选</button></div><div class="meta" id="meta"></div></header>
<main><div class="grid" id="grid"></div><div class="empty" id="empty" hidden>没有匹配的图标</div><button class="more" id="more" hidden>加载更多</button></main>
<script>
const data="""
        + payload
        + """;
const step=240;let filtered=data,visible=step;
const grid=document.querySelector('#grid'),query=document.querySelector('#query'),category=document.querySelector('#category'),meta=document.querySelector('#meta'),more=document.querySelector('#more'),empty=document.querySelector('#empty');
[...new Set(data.map(x=>x.category))].sort((a,b)=>a.localeCompare(b)).forEach(x=>{const o=document.createElement('option');o.value=x;o.textContent=x;category.append(o)});
function render(){grid.replaceChildren();const fragment=document.createDocumentFragment();for(const item of filtered.slice(0,visible)){const card=document.createElement('article');card.className='card';const link=document.createElement('a');link.className='preview';link.href=item.url;link.target='_blank';const img=document.createElement('img');img.loading='lazy';img.src=item.url;img.alt=item.name;link.append(img);const info=document.createElement('div');info.className='info';const name=document.createElement('div');name.className='name';name.title=item.name;name.textContent=item.name;const path=document.createElement('div');path.className='path';path.title=item.path;path.textContent=item.path;const dim=document.createElement('div');dim.className='dim';dim.textContent=`${item.width}×${item.height} · ${(item.bytes/1024).toFixed(1)} KiB`;info.append(name,path,dim);card.append(link,info);fragment.append(card)}grid.append(fragment);empty.hidden=filtered.length!==0;more.hidden=visible>=filtered.length;meta.textContent=`显示 ${Math.min(visible,filtered.length).toLocaleString()} / ${filtered.length.toLocaleString()}，总计 ${data.length.toLocaleString()} 张 PNG`}
function apply(){const term=query.value.trim().toLowerCase(),cat=category.value;filtered=data.filter(x=>(!cat||x.category===cat)&&(!term||x.path.toLowerCase().includes(term)));visible=step;render()}
query.addEventListener('input',apply);category.addEventListener('change',apply);more.addEventListener('click',()=>{visible+=step;render()});document.querySelector('#reset').addEventListener('click',()=>{query.value='';category.value='';apply()});render();
</script></body></html>""",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Exported PNG root")
    parser.add_argument("--output", type=Path, required=True, help="Catalog HTML path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_root = args.input.resolve()
    output_file = args.output.resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    rows = collect_images(input_root, output_file)
    write_html(rows, output_file)
    csv_file = write_csv(rows, output_file)
    print(f"Cataloged {len(rows)} PNG files: {output_file}")
    print(f"Inventory: {csv_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .api import render
from .fonts import FontSet
from .model import Block, Limits, RenderError
from .parser import parse


def local_assets(markdown: str, root: Path, limits: Limits) -> dict[str, bytes]:
    root = root.resolve(strict=True)
    sources: set[str] = set()

    def walk(block: Block) -> None:
        sources.update(span.target for span in block.inlines if span.kind == "image")
        for child in block.children:
            walk(child)

    for block in parse(markdown, limits).children:
        walk(block)
    if len(sources) > limits.max_assets:
        raise RenderError("Asset count budget exceeded")
    result = {}
    for source in sources:
        if urlsplit(source).scheme or source.startswith("//"):
            continue  # Never fetch remote/data/file URLs.
        path = (root / source).resolve()
        if not path.is_relative_to(root):
            raise RenderError(f"Asset escapes allowed root: {source}")
        if path.is_file():
            with path.open("rb") as file:
                data = file.read(limits.max_asset_bytes + 1)
            if len(data) > limits.max_asset_bytes:
                raise RenderError(f"Asset byte budget exceeded: {source}")
            result[source] = data
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown → measured display list → Pillow PNG")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=840)
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--fonts", type=Path, help="Explicit font manifest JSON")
    parser.add_argument(
        "--asset-root", type=Path, help="Opt in to bounded local image reads under this root"
    )
    parser.add_argument(
        "--debug", type=Path, help="Cumulative paint frames, layout JSON, baseline overlay"
    )
    args = parser.parse_args()
    try:
        limits = Limits()
        with args.input.open(encoding="utf-8") as file:
            markdown = file.read(limits.max_chars + 1)
        result = render(
            markdown,
            width=args.width,
            scale=args.scale,
            fonts=FontSet.load(args.fonts) if args.fonts else None,
            assets=local_assets(markdown, args.asset_root, limits) if args.asset_root else {},
            debug=args.debug,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.image.save(args.output)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "size": result.image.size,
                    "components": len(result.scene.components),
                    "diagnostics": [
                        {"code": d.code, "message": d.message, "component": d.component}
                        for d in result.scene.diagnostics
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (RenderError, OSError, ValueError) as exc:
        print(f"md2png: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

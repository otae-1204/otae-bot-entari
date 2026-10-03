"""Offline CLI. Favicons must be explicitly supplied, not fetched."""

import argparse
import json
from pathlib import Path

from . import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--debug", type=Path)
    parser.add_argument("--favicon", type=Path)
    parser.add_argument("--scale", type=int, default=2)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text())
    assets = {}
    if args.favicon:
        raw = args.favicon.read_bytes()
        for ref in payload.get("references", []) + payload.get("page_references", []):
            assets["favicon:" + ref["url"]] = raw
    result = render(payload, assets=assets, scale=args.scale, debug=args.debug)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.image.save(args.output)
    print(json.dumps({"size": result.image.size, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

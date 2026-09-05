from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.endfield.providers.warfarin import WarfarinClient
from plugins.endfield.calendar.official import OfficialVersionCalendarSource
from plugins.endfield.calendar.official_draw import draw_official_version_calendar
from plugins.endfield.calendar.akedata import AkeDataVersionCalendarSource
from plugins.endfield.calendar.draw import draw_version_calendar


async def render(output: Path, *, generated: bool = False) -> None:
    if generated:
        source = AkeDataVersionCalendarSource(WarfarinClient())
        calendar = await source.current()
        image = await draw_version_calendar(calendar)
        label = f"Endfield {calendar.version} generated calendar ({calendar.revision})"
    else:
        calendar = await OfficialVersionCalendarSource().current()
        image = await draw_official_version_calendar(calendar)
        label = f"Endfield official calendar ({calendar.revision})"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(image)
    print(f"Rendered {label} -> {output} ({len(image)} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the data-driven Endfield version calendar.")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=ROOT / ".runtime" / "endfield_calendar" / "version_calendar.png",
    )
    parser.add_argument(
        "--generated",
        action="store_true",
        help="Render the AkeData/HTML fallback instead of the official-site assets.",
    )
    args = parser.parse_args()
    asyncio.run(render(args.output.resolve(), generated=args.generated))


if __name__ == "__main__":
    main()

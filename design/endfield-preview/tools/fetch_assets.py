"""Download a small, fixed set of public game sprites; never generate imagery."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
OPERATORS = {
    "laevatain": "chr_0016_laevat",
    "endministrator": "chr_0003_endminf",
    "perlica": "chr_0004_pelica",
    "chen": "chr_0005_chen",
    "gilberta": "chr_0013_aglina",
    "yvonne": "chr_0017_yvonne",
    "wulfgard": "chr_0006_wolfgd",
    "arclight": "chr_0007_ikut",
}
ASSETS = {
    **{name: f"charremoteicon/icon_{key}.png" for name, key in OPERATORS.items()},
    "laevatain-art": "characterportrait/chr_0016_laevat.png",
    "weapon-0": "itemiconbig/wpn_sword_0006.png",
    "weapon-1": "itemiconbig/wpn_sword_0016.png",
    "weapon-2": "itemiconbig/wpn_claym_0013.png",
    "weapon-3": "itemiconbig/wpn_funnel_0009.png",
    "gear-hand": "itemiconbig/item_equip_t4_suit_combo_cd01_hand_01.png",
    "gear-body": "itemiconbig/item_equip_t4_suit_criti01_body_06.png",
    "gear-accessory": "itemiconbig/item_equip_t4_suit_attri01_edc_03.png",
    "gear-accessory2": "itemiconbig/item_equip_t4_suit_atb01_edc_01.png",
    "medal-1": "medaliconbig/achv_adv_tundra_box_lv01.png",
    "medal-2": "medaliconbig/achv_adv_tundra_box_lv02.png",
    "medal-3": "medaliconbig/achv_adv_tundra_box_lv03.png",
}


def fetch(entry):
    name, relative = entry
    target = ROOT / "assets" / f"{name}.webp"
    if target.exists():
        return name
    request = Request(
        f"{BASE}/{relative}", headers={"User-Agent": "otae-ui-preview/1.0"}
    )
    with urlopen(request, timeout=45) as response:
        content = response.read(32 * 1024 * 1024 + 1)
    if len(content) > 32 * 1024 * 1024:
        raise ValueError(f"Oversized public asset: {name}")
    with Image.open(BytesIO(content)) as source:
        image = source.convert("RGBA")
        image.thumbnail((1500, 1500) if name.endswith("-art") else (520, 520))
        image.save(target, "WEBP", quality=88, method=6)
    return name


if __name__ == "__main__":
    (ROOT / "assets").mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name in pool.map(fetch, ASSETS.items()):
            print(f"Ready: {name}", flush=True)

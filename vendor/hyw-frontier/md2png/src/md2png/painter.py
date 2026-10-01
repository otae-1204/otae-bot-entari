"""Display-list replay only: no Markdown parsing, line breaking or text remeasurement."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .fonts import Fonts
from .model import RenderError
from .resources import Budget, Resources
from .scene import Op, Scene, Theme

LAYERS = ("background", "panels", "content", "decoration")


def paint(
    scene: Scene,
    fonts: Fonts,
    resources: Resources,
    theme: Theme,
    budget: Budget,
    frames: Path | None = None,
) -> Image.Image:
    budget.check(scene.height, scene.width, scene.scale)
    scale = scene.scale
    canvas = Image.new("RGBA", (scene.width * scale, scene.height * scale), theme.page)
    if frames:
        frames.mkdir(parents=True, exist_ok=True)

    def draw_op(op: Op) -> None:
        budget.check()
        b = op.box
        if b.width < 0 or b.height < 0:
            raise RenderError(f"Negative display-list extent: {op.component}")
        box = tuple(round(v * scale) for v in (b.x, b.y, b.x + b.width, b.y + b.height))
        # Layout boxes are half-open; Pillow rectangles have inclusive end pixels.
        rectangle = (box[0], box[1], max(box[0], box[2] - 1), max(box[1], box[3] - 1))
        draw = ImageDraw.Draw(canvas)
        if op.kind == "rect":
            draw.rounded_rectangle(
                rectangle,
                radius=round(op.radius * scale),
                fill=op.fill or None,
                outline=op.stroke or None,
                width=max(1, round(op.stroke_width * scale)),
            )
        elif op.kind == "shadow":
            pad = 18 * scale
            shadow = Image.new("RGBA", (box[2] - box[0] + pad * 2, box[3] - box[1] + pad * 2))
            ImageDraw.Draw(shadow).rounded_rectangle(
                (pad, pad, shadow.width - pad - 1, shadow.height - pad - 1),
                radius=round(op.radius * scale),
                fill=op.fill,
            )
            shadow = shadow.filter(ImageFilter.GaussianBlur(op.blur * scale))
            canvas.alpha_composite(shadow, (box[0] - pad, box[1] - pad))
        elif op.kind == "text":
            if op.face is None:
                raise RenderError("Text paint operation has no face")
            fonts.draw_text(
                canvas, b.x, op.baseline, op.text, op.face, op.size, op.fill, op.tracking
            )
        elif op.kind == "line":
            draw.line(box, fill=op.fill, width=max(1, round(op.stroke_width * scale)))
        elif op.kind == "image":
            target = (max(1, box[2] - box[0]), max(1, box[3] - box[1]))
            image = resources.images[op.asset]
            if image.size != target:
                image = image.resize(target, Image.Resampling.LANCZOS)
            canvas.alpha_composite(image, (box[0], box[1]))
        else:
            raise RenderError(f"Unknown paint operation {op.kind}")

    for index, layer in enumerate(LAYERS):
        for op in scene.ops:
            if op.layer == layer:
                draw_op(op)
        if frames:
            canvas.resize((scene.width, scene.height), Image.Resampling.LANCZOS).save(
                frames / f"{index:02d}-{layer}.png"
            )
    budget.check()
    if scale != 1:
        canvas = canvas.resize((scene.width, scene.height), Image.Resampling.LANCZOS)
    return canvas.convert("RGB")

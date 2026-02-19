from __future__ import annotations

from io import BytesIO
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont # type: ignore[import]


def render_activity_chart(
    counts: Sequence[int],
    labels: Sequence[str],
    *,
    width: int = 900,
    height: int = 420,
) -> BytesIO:
    """Render a simple line chart for activity counts."""
    img = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    left = 60
    right = 20
    top = 20
    bottom = 60

    chart_w = width - left - right
    chart_h = height - top - bottom

    # Axes
    draw.line((left, top, left, top + chart_h), fill="#222222", width=2)
    draw.line((left, top + chart_h, left + chart_w, top + chart_h), fill="#222222", width=2)

    max_val = max(counts) if counts else 0
    if max_val <= 0:
        max_val = 1

    # Grid + Y labels
    grid_lines = 4
    for i in range(1, grid_lines + 1):
        y = top + chart_h - int(chart_h * i / grid_lines)
        draw.line((left, y, left + chart_w, y), fill="#EEEEEE", width=1)
        val = int(max_val * i / grid_lines)
        draw.text((8, y - 6), str(val), fill="#666666", font=font)

    n = len(counts)
    if n == 0:
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    # Line
    prev = None
    for i, val in enumerate(counts):
        x = left + int(chart_w * i / max(1, n - 1))
        y = top + chart_h - int(chart_h * val / max_val)
        if prev is not None:
            draw.line((prev[0], prev[1], x, y), fill="#2A6F97", width=2)
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill="#2A6F97")
        prev = (x, y)

    # X labels
    if labels:
        if n <= 8:
            step = 1
        elif n <= 16:
            step = 2
        elif n <= 24:
            step = 3
        else:
            step = 5
        for i, lbl in enumerate(labels):
            if i % step == 0 or i == n - 1:
                x = left + int(chart_w * i / max(1, n - 1))
                draw.text((x - 10, top + chart_h + 8), str(lbl), fill="#666666", font=font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

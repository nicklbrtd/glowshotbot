import hashlib
import io
import os
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps  # type: ignore[import]


_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_PREFIX = "GS-"


def _to_base(value: int, length: int = 6) -> str:
    base = len(_ALPHABET)
    chars = []
    v = value
    for _ in range(length):
        v, rem = divmod(v, base)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def generate_author_code(user_id: int, salt: str) -> str:
    """
    Детерминированно генерирует код автора вида GS-XXXXXX.
    Основан на хеше (salt + user_id) и алфавите без неоднозначных символов.
    """
    payload = f"{salt}|{int(user_id)}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    num = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return f"{_PREFIX}{_to_base(num)}"


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        try:
            return ImageFont.truetype("arial.ttf", size=size)
        except Exception:
            return ImageFont.load_default()


def _calc_text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        w, h = draw.textsize(text, font=font)
        return w, h


def _split_colored_parts(
    text: str,
    *,
    highlight_text: str | None = None,
    base_color: tuple[int, int, int, int] = (255, 255, 255, 24),
    accent_color: tuple[int, int, int, int] = (255, 140, 0, 24),
) -> list[tuple[str, tuple[int, int, int, int]]]:
    if not text:
        return []
    if not highlight_text:
        return [(text, base_color)]
    needle = str(highlight_text).strip()
    if not needle:
        return [(text, base_color)]
    idx = text.find(needle)
    if idx < 0:
        return [(text, base_color)]
    out: list[tuple[str, tuple[int, int, int, int]]] = []
    if idx > 0:
        out.append((text[:idx], base_color))
    out.append((needle, accent_color))
    right = text[idx + len(needle) :]
    if right:
        out.append((right, base_color))
    return out


def _render_rgba_with_watermark(
    image_bytes: bytes,
    text: str,
    *,
    highlight_text: str | None = None,
    max_side: int = 2560,
) -> tuple[Image.Image, str]:
    src = Image.open(io.BytesIO(image_bytes))
    src_format = (src.format or "").upper()
    src = ImageOps.exif_transpose(src)
    if src.mode != "RGBA":
        src = src.convert("RGBA")

    width, height = src.size
    longest = max(width, height)
    if longest > max_side > 0:
        ratio = float(max_side) / float(longest)
        width = max(1, int(width * ratio))
        height = max(1, int(height * ratio))
        src = src.resize((width, height), Image.Resampling.LANCZOS)
    else:
        width, height = src.size

    overlay = Image.new("RGBA", src.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    parts = _split_colored_parts(text, highlight_text=highlight_text)

    font_size = int(max(12, min(52, width * 0.028)))
    font = _load_font(font_size)

    while font_size > 10:
        part_sizes: list[tuple[int, int]] = []
        total_w = 0
        max_h = 0
        for part, _ in parts:
            w, h = _calc_text_size(draw, part, font)
            total_w += w
            max_h = max(max_h, h)
            part_sizes.append((w, h))
        if total_w <= int(width * 0.94):
            break
        font_size -= 1
        font = _load_font(font_size)

    part_sizes = []
    total_w = 0
    max_h = 0
    for part, _ in parts:
        w, h = _calc_text_size(draw, part, font)
        total_w += w
        max_h = max(max_h, h)
        part_sizes.append((w, h))

    x = max(2, (width - total_w) // 2)
    bottom_offset = max(6, int(height * 0.04))
    y = max(2, height - bottom_offset - max_h)

    shadow_fill = (0, 0, 0, 14)
    cur_x = x
    for idx, (part, color) in enumerate(parts):
        if not part:
            continue
        part_w, _ = part_sizes[idx]
        draw.text((cur_x + 1, y + 1), part, font=font, fill=shadow_fill)
        draw.text((cur_x, y), part, font=font, fill=color)
        cur_x += part_w

    composed = Image.alpha_composite(src, overlay)
    return composed, src_format


def apply_text_watermark(
    image_bytes: bytes,
    text: str,
    *,
    highlight_text: str | None = None,
    max_side: int = 4096,
) -> bytes:
    """
    Наносит водяной знак внизу по центру с низкой прозрачностью.
    Возвращает готовые байты изображения (с сохранением формата, если возможно).
    """
    if not image_bytes or not text:
        return image_bytes

    try:
        composed, src_format = _render_rgba_with_watermark(
            image_bytes,
            text,
            highlight_text=highlight_text,
            max_side=int(max_side),
        )
        out = io.BytesIO()
        if src_format == "PNG":
            composed.save(out, format="PNG", optimize=True)
        elif src_format == "WEBP":
            composed.save(out, format="WEBP", quality=95, method=6)
        else:
            composed.convert("RGB").save(
                out,
                format="JPEG",
                quality=95,
                subsampling=0,
                optimize=True,
                progressive=True,
            )
        return out.getvalue()
    except Exception:
        return image_bytes

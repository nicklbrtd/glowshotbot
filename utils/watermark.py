import hashlib
import io
import os
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont # type: ignore[import]


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


def apply_text_watermark(image_bytes: bytes, text: str) -> bytes:
    """
    Наносит текстовый водяной знак (белый текст с тенью) на изображение.
    Обновлённый стиль: нижний центр, мягкая полупрозрачная плашка и тень.
    - Масштабирует до max 2048px по длинной стороне.
    - Размер шрифта: ~2.3% ширины (clamp 14..42).
    - Добавляет лёгкий тёмный blur‑фон под текстом для читаемости.
    Возвращает байты JPEG с качеством 95 и subsampling=0.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    # Масштабируем для экономии ресурсов
    max_side = 2048
    w, h = img.size
    if max(w, h) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        w, h = img.size

    font_size = max(14, min(42, int(w * 0.023)))
    font = _load_font(font_size)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    text = text.strip()
    if not text:
        text = "GlowShot"

    text_w, text_h = _calc_text_size(draw, text, font)
    margin_x = int(w * 0.04)
    margin_y = int(h * 0.035)

    x = max(margin_x, (w - text_w) // 2)
    y = max(margin_y, h - text_h - margin_y)

    # Мягкая подложка под текст
    pad = int(font_size * 0.5)
    bg_rect = (x - pad, y - pad, x + text_w + pad, y + text_h + pad)
    draw.rounded_rectangle(bg_rect, radius=int(pad * 0.6), fill=(0, 0, 0, 55))

    shadow_offset = max(1, font_size // 12)
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=(0, 0, 0, 70))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 165))

    stamped = Image.alpha_composite(img, overlay).convert("RGB")
    out = io.BytesIO()
    stamped.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
    return out.getvalue()

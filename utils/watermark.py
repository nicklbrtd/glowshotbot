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
    Временный заглушечный режим: возвращаем оригинальное изображение без водяного знака.
    TODO: вернуть новый стиль watermark + «2026 All rights Reserved».
    """
    return image_bytes

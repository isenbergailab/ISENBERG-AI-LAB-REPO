"""Branded text cards; no member images are accepted."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import hashlib

BLUE = "#1F3FC4"
INK = "#1D1B18"
PAPER = "#E9E4D8"
BRICK = "#8C2B22"


def _font(assets: Path, name: str, size: int):
    target = assets / name
    return ImageFont.truetype(str(target), size) if target.exists() else ImageFont.load_default(size=size)


def _lines(draw, text, font, width):
    words, rows, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] > width and current:
            rows.append(current)
            current = word
        else:
            current = candidate
    if current:
        rows.append(current)
    return rows


def render_card(card: dict, output: Path, assets: Path, size: tuple[int, int], seed: str) -> None:
    width, height = size
    mode = int(hashlib.sha256(seed.encode()).hexdigest()[:2], 16) % 3
    background = PAPER if mode < 2 else BLUE
    foreground = BLUE if mode < 2 else PAPER
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    margin = int(width * 0.075)
    stripe = int(width * 0.024)
    draw.rectangle((0, 0, stripe, height), fill=BRICK if mode == 1 else foreground)
    eyebrow_font = _font(assets, "JetBrainsMono.ttf", int(width * 0.021))
    title_font = _font(assets, "BricolageGrotesque.ttf", int(width * 0.073))
    body_font = _font(assets, "Onest.ttf", int(width * 0.033))
    draw.text((margin, margin), "ISENBERG AI LAB  /  UMASS AMHERST", font=eyebrow_font, fill=foreground)
    rule_y = margin + int(width * 0.052)
    draw.rectangle((margin, rule_y, width - margin, rule_y + 3), fill=foreground)
    title_rows = _lines(draw, card["title"], title_font, width - 2 * margin)
    title_y = int(height * 0.30)
    line_height = int(width * 0.09)
    for row in title_rows[:5]:
        draw.text((margin, title_y), row, font=title_font, fill=foreground)
        title_y += line_height
    subtitle_y = max(title_y + int(width * 0.05), int(height * 0.61))
    for row in _lines(draw, card["subtitle"], body_font, width - 2 * margin)[:5]:
        draw.text((margin, subtitle_y), row, font=body_font, fill=foreground)
        subtitle_y += int(width * 0.05)
    footer_y = height - margin - int(width * 0.04)
    draw.rectangle((margin, footer_y - int(width * 0.04), width - margin, footer_y - int(width * 0.04) + 3), fill=foreground)
    draw.text((margin, footer_y), "BUILD  /  TEST  /  DISCUSS", font=eyebrow_font, fill=foreground)
    image.save(output, optimize=True)

"""Rebuild the PLS Store series: python3 tools/render_banners.py (Pillow required)."""
from pathlib import Path
import json
import argparse
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "design/banners"
ASSETS = ROOT / "PLS-Store/assets"
SIZE = (1600, 900)
BRAND_POSITION = (72, 56)
BRAND_SIZE = (480, 140)


def font(size, bold=False):
    return ImageFont.truetype(str(DESIGN / "fonts" / (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def make_brand():
    # Extract the original lockup, including its original lettering and divider.
    # No generated or redrawn logo: retain original source RGB, remove dark matte.
    brand = Image.open(DESIGN / "sources/reference.png").convert("RGBA").crop((70, 49, 390, 142))
    pixels = []
    for r, g, b, _ in list(brand.get_flattened_data()):
        alpha = round(255 * max(0, min(1, (max(r, g, b) - 35) / 35)))
        pixels.append((r, g, b, alpha))
    brand.putdata(pixels)
    brand = brand.resize(BRAND_SIZE, Image.Resampling.LANCZOS)
    brand.save(DESIGN / "brand-lockup.png")
    return brand


def render(only=None, lang="ru"):
    spec = json.loads((DESIGN / "series.json").read_text())
    if lang != "ru":
        translations = json.loads((DESIGN / "translations.json").read_text())[lang]
        assert set(translations) == {item["key"] for item in spec}
        spec = [dict(item, **translations[item["key"]]) for item in spec]
    output_dir = ASSETS if lang == "ru" else ASSETS / lang
    output_dir.mkdir(parents=True, exist_ok=True)
    brand = make_brand()
    thumbs = []
    for item in spec:
        if only and item["key"] not in only:
            continue
        atlas = Image.open(DESIGN / "sources" / f"atlas-{item['atlas']}.png").convert("RGB")
        w, h = atlas.size
        col, row = item["cell"]
        background = atlas.crop((col*w//2, row*h//2, (col+1)*w//2, (row+1)*h//2)).resize(SIZE, Image.Resampling.LANCZOS)
        background.save(DESIGN / "sources" / f"{item['key']}-background.png")
        canvas = background.convert("RGBA")
        canvas.alpha_composite(brand, BRAND_POSITION)
        draw = ImageDraw.Draw(canvas)
        y = 292
        for line in item["title"]:
            assert draw.textbbox((0, 0), line, font=font(96, True))[2] <= 760, line
            draw.text((72, y), line, font=font(96, True), fill="white", stroke_width=0)
            y += 116
        y += 28
        for paragraph in item["subtitle"]:
            lines = [""]
            for word in paragraph.split():
                candidate = (lines[-1] + " " + word).strip()
                if font(48).getlength(candidate) > 700 and lines[-1]:
                    lines.append(word)
                else:
                    lines[-1] = candidate
            for line in lines:
                assert draw.textbbox((0, 0), line, font=font(48))[2] <= 700, line
                draw.text((76, y), line, font=font(48), fill="#ddc6f4")
                y += 60
        assert y < 715
        out = output_dir / item["file"]
        canvas.convert("RGB").save(out, optimize=True)
        box = (*BRAND_POSITION, BRAND_POSITION[0]+BRAND_SIZE[0], BRAND_POSITION[1]+BRAND_SIZE[1])
        expected = background.crop(box).convert("RGBA")
        expected.alpha_composite(brand)
        assert Image.open(out).crop(box).tobytes() == expected.convert("RGB").tobytes()
        # All fully opaque brand pixels have exactly the same RGB in every file.
        opaque = brand.getchannel("A").point(lambda a: 255 if a == 255 else 0)
        exported = Image.open(out).crop(box)
        assert Image.composite(exported, brand.convert("RGB"), opaque).tobytes() == brand.convert("RGB").tobytes()
    for item in spec:
        thumb = Image.open(output_dir / item["file"]).convert("RGB").resize((480, 270), Image.Resampling.LANCZOS)
        thumbs.append(thumb)
    sheet = Image.new("RGB", (1440, 1080), "#0f031c")
    for i, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((i % 3)*480, (i // 3)*270))
    sheet_name = "contact-sheet.jpg" if lang == "ru" else f"contact-sheet-{lang}.jpg"
    sheet.save(DESIGN / sheet_name, quality=93)
    count = sum(not only or item["key"] in only for item in spec)
    print(f"{lang}: rendered and verified {count} banners, {SIZE[0]}×{SIZE[1]}; identical brand layer and opaque pixels.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", help="Rebuild selected section keys")
    parser.add_argument("--languages", nargs="+", choices=("ru", "en", "uk"), default=["ru", "en", "uk"])
    args = parser.parse_args()
    for language in args.languages:
        render(args.only, language)

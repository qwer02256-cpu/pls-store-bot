"""Reproducible 1600x900 PLS Store banners; requires Pillow, not a bot dependency."""
from pathlib import Path
import argparse
import hashlib
import json
import math
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / 'design/banners'
ASSETS = ROOT / 'PLS-Store/assets'
LAYOUT = json.loads((DESIGN / 'layout.json').read_text())
SIZE = tuple(LAYOUT['size'])
BRAND_POSITION = tuple(LAYOUT['brand_position'])
BRAND_SIZE = tuple(LAYOUT['brand_size'])


def font(size, bold=False):
    return ImageFont.truetype(str(DESIGN / 'fonts' / ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')), size)


def make_brand():
    path = DESIGN / 'brand-lockup.png'
    if path.exists():
        brand = Image.open(path).convert('RGBA')
        assert brand.size == BRAND_SIZE
        return brand
    # Recreate only if the common original asset is missing, never per banner.
    brand = Image.open(DESIGN / 'sources/reference.png').convert('RGBA').crop((70, 49, 390, 142))
    pixels = []
    for r, g, b, _ in brand.getdata():
        alpha = round(255 * max(0, min(1, (max(r, g, b) - 35) / 35)))
        pixels.append((r, g, b, alpha))
    brand.putdata(pixels)
    brand = brand.resize(BRAND_SIZE, Image.Resampling.LANCZOS)
    brand.save(path)
    return brand


def wrap(paragraph, face, width):
    lines = ['']
    for word in paragraph.split():
        candidate = (lines[-1] + ' ' + word).strip()
        if face.getlength(candidate) > width and lines[-1]:
            lines.append(word)
        else:
            lines[-1] = candidate
    assert all(face.getlength(line) <= width for line in lines), paragraph
    return lines


def render(only=None, lang='ru'):
    spec = json.loads((DESIGN / 'series.json').read_text())
    if lang != 'ru':
        translations = json.loads((DESIGN / 'translations.json').read_text())[lang]
        assert set(translations) == {item['key'] for item in spec}
        spec = [dict(item, **translations[item['key']]) for item in spec]
    output_dir = ASSETS if lang == 'ru' else ASSETS / lang
    output_dir.mkdir(parents=True, exist_ok=True)
    brand = make_brand()
    report = []
    box = (*BRAND_POSITION, BRAND_POSITION[0] + BRAND_SIZE[0], BRAND_POSITION[1] + BRAND_SIZE[1])
    opaque = brand.getchannel('A').point(lambda a: 255 if a == 255 else 0)
    for item in spec:
        if only and item['key'] not in only:
            continue
        source = DESIGN / 'sources' / f"{item['key']}-background.png"
        assert source.is_file(), f'Missing generated illustration: {source}'
        background = Image.open(source).convert('RGB').resize(SIZE, Image.Resampling.LANCZOS)
        canvas = background.convert('RGBA')
        canvas.alpha_composite(brand, BRAND_POSITION)
        draw = ImageDraw.Draw(canvas)
        x, y = LAYOUT['title_position']
        for paragraph in item['title']:
            for line in wrap(paragraph, font(LAYOUT['title_size'], True), LAYOUT['title_width']):
                draw.text((x, y), line, font=font(LAYOUT['title_size'], True), fill='white')
                y += LAYOUT['title_step']
        y += LAYOUT['subtitle_gap']
        for paragraph in item['subtitle']:
            for line in wrap(paragraph, font(LAYOUT['subtitle_size']), LAYOUT['subtitle_width']):
                draw.text((LAYOUT['subtitle_x'], y), line, font=font(LAYOUT['subtitle_size']), fill=LAYOUT['subtitle_color'])
                y += LAYOUT['subtitle_step']
        assert y < LAYOUT['text_bottom'], (item['key'], lang, y)
        out = output_dir / item['file']
        canvas.convert('RGB').save(out, compress_level=6)
        exported = Image.open(out)
        expected = background.crop(box).convert('RGBA')
        expected.alpha_composite(brand)
        assert exported.crop(box).tobytes() == expected.convert('RGB').tobytes()
        assert Image.composite(exported.crop(box), brand.convert('RGB'), opaque).tobytes() == brand.convert('RGB').tobytes()
        report.append({'key': item['key'], 'file': str(out.relative_to(ROOT)), 'size': list(exported.size),
                       'sha256': hashlib.sha256(out.read_bytes()).hexdigest(), 'brand_layer_verified': True})
    thumbs = [Image.open(output_dir / item['file']).convert('RGB').resize((480, 270), Image.Resampling.LANCZOS) for item in spec]
    sheet = Image.new('RGB', (1440, math.ceil(len(thumbs) / 3) * 270), '#0f031c')
    for i, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((i % 3) * 480, (i // 3) * 270))
    sheet.save(DESIGN / ('contact-sheet.jpg' if lang == 'ru' else f'contact-sheet-{lang}.jpg'), quality=93)
    (DESIGN / f'build-report-{lang}.json').write_text(json.dumps({'language': lang, 'brand_position': BRAND_POSITION,
        'brand_size': BRAND_SIZE, 'brand_sha256': hashlib.sha256((DESIGN / 'brand-lockup.png').read_bytes()).hexdigest(),
        'banners': report}, ensure_ascii=False, indent=2) + '\n')
    print(f'{lang}: {len(report)} banners at {SIZE}; shared brand layer verified at {BRAND_POSITION}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', nargs='+')
    parser.add_argument('--languages', nargs='+', choices=('ru', 'en', 'uk'), default=['ru', 'en', 'uk'])
    args = parser.parse_args()
    for language in args.languages:
        render(args.only, language)

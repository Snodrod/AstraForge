import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

CELL = 768
HEADER = 42


def font():
    for name in ('arial.ttf', 'segoeui.ttf'):
        try:
            return ImageFont.truetype('C:/Windows/Fonts/' + name, 22)
        except OSError:
            continue
    return ImageFont.load_default()


def contain(image, size):
    image = image.convert('RGB')
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    panel = Image.new('RGB', (size, size), (128, 128, 128))
    panel.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
    return panel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--generated', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = Path(args.source)
    generated = Path(args.generated)
    output = Path(args.output)
    if not source.is_file() or not generated.is_file():
        raise SystemExit('Both source and generated images are required.')
    sheet = Image.new('RGB', (CELL * 2, HEADER + CELL), (28, 30, 34))
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 9), 'SOURCE', fill=(240, 242, 246), font=font())
    draw.text((CELL + 12, 9), 'A-POSE', fill=(240, 242, 246), font=font())
    with Image.open(source) as image:
        sheet.paste(contain(image, CELL), (0, HEADER))
    with Image.open(generated) as image:
        sheet.paste(contain(image, CELL), (CELL, HEADER))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=92)
    print(f'RESULT {output.resolve()}')


if __name__ == '__main__':
    main()

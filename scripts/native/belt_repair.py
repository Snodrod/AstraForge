from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


SLOTS = ('front', 'left', 'back', 'right')


def rectangle_mask(size, rectangles):
    if not rectangles:
        raise RuntimeError('A non-empty mask rectangle list is required.')
    width, height = size
    mask = Image.new('L', size, 0)
    draw = ImageDraw.Draw(mask)
    for rectangle in rectangles:
        if len(rectangle) != 4:
            raise RuntimeError('Each mask rectangle must contain four normalized coordinates.')
        x0, y0, x1, y1 = (float(value) for value in rectangle)
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise RuntimeError(f'Invalid normalized mask rectangle: {rectangle}')
        left = math.floor(x0 * width)
        top = math.floor(y0 * height)
        right = math.ceil(x1 * width)
        bottom = math.ceil(y1 * height)
        draw.rectangle((left, top, right - 1, bottom - 1), fill=255)
    if mask.getbbox() is None:
        raise RuntimeError('The effective mask is empty.')
    return mask


def effective_mask(size, rectangles, feather_px=0):
    hard = rectangle_mask(size, rectangles)
    if feather_px <= 0:
        return hard
    softened = hard.filter(ImageFilter.GaussianBlur(float(feather_px)))
    return ImageChops.darker(softened, hard)


def composite_view(source_path, donor_path, mask, output_path, background_mask=None):
    with Image.open(source_path) as source_image, Image.open(donor_path) as donor_image:
        source = source_image.convert('RGB')
        donor = donor_image.convert('RGB')
    if source.size != donor.size or source.size != mask.size:
        raise RuntimeError('Source, donor and mask must have the same dimensions.')
    result = Image.composite(donor, source, mask)
    combined_mask = mask
    background_pixels = 0
    if background_mask is not None:
        if background_mask.size != source.size:
            raise RuntimeError('Source, donor and mask must have the same dimensions.')
        background = Image.new('RGB', source.size, source.getpixel((0, 0)))
        result = Image.composite(background, result, background_mask)
        combined_mask = ImageChops.lighter(mask, background_mask)
        background_pixels = sum(weight > 0 for weight in background_mask.tobytes())
    difference = ImageChops.difference(source, result)
    outside_changed = 0
    inside_changed = 0
    mask_pixels = 0
    difference_bytes = difference.tobytes()
    for index, weight in enumerate(combined_mask.tobytes()):
        offset = index * 3
        changed = any(difference_bytes[offset:offset + 3])
        if weight == 0:
            if changed:
                outside_changed += 1
        else:
            mask_pixels += 1
            if changed:
                inside_changed += 1
    if outside_changed:
        raise RuntimeError(f'Composite changed {outside_changed} pixels outside the effective mask.')
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path)
    return {
        'outsideChangedPixels': outside_changed,
        'insideChangedPixels': inside_changed,
        'maskPixels': mask_pixels,
        'backgroundMaskPixels': background_pixels,
        'width': source.width,
        'height': source.height,
    }


def load_config(config_path):
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    if config.get('version') != 1:
        raise RuntimeError('Belt repair config version must be 1.')
    if set(config.get('masks', {})) != set(SLOTS):
        raise RuntimeError('Belt repair config must define masks for front, left, back and right.')
    if not set(config.get('backgroundMasks', {})).issubset(SLOTS):
        raise RuntimeError('Background masks contain an unknown view.')
    feather = config.get('featherPx', 0)
    if not isinstance(feather, (int, float)) or not 0 <= feather <= 32:
        raise RuntimeError('featherPx must be in 0..32.')
    return config


def prepare_masks(source_views, output_attempt, config):
    source_views = Path(source_views).resolve()
    output_attempt = Path(output_attempt).resolve()
    masks = {}
    background_masks = {}
    previews = []
    for slot in SLOTS:
        source_path = source_views / 'raw' / f'{slot}.png'
        if not source_path.is_file():
            raise RuntimeError(f'Source view is missing: {source_path}')
        with Image.open(source_path) as image:
            source = image.convert('RGB')
        mask = effective_mask(source.size, config['masks'][slot], config.get('featherPx', 0))
        mask_path = output_attempt / 'masks' / f'{slot}.png'
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        mask.save(mask_path)
        masks[slot] = mask
        background_mask = None
        if slot in config.get('backgroundMasks', {}):
            background_mask = effective_mask(source.size, config['backgroundMasks'][slot], config.get('backgroundFeatherPx', config.get('featherPx', 0)))
            background_mask.save(output_attempt / 'masks' / f'background-{slot}.png')
            background_masks[slot] = background_mask

        preview = source.copy()
        red = Image.new('RGB', source.size, (255, 32, 32))
        tint = Image.composite(red, preview, mask.point(lambda value: int(value * 0.42)))
        preview = Image.blend(preview, tint, 0.65)
        if background_mask is not None:
            blue = Image.new('RGB', source.size, (32, 96, 255))
            tint = Image.composite(blue, preview, background_mask.point(lambda value: int(value * 0.65)))
            preview = Image.blend(preview, tint, 0.65)
        preview.thumbnail((384, 384), Image.Resampling.LANCZOS)
        tile = Image.new('RGB', (384, 416), (24, 24, 24))
        tile.paste(preview, ((384 - preview.width) // 2, 32 + (384 - preview.height) // 2))
        ImageDraw.Draw(tile).text((12, 10), f'{slot} — red = donor, blue = background', fill=(255, 255, 255))
        previews.append(tile)
    sheet = Image.new('RGB', (384 * len(previews), 416), (24, 24, 24))
    for index, preview in enumerate(previews):
        sheet.paste(preview, (384 * index, 0))
    sheet_path = output_attempt / 'mask-preview.jpg'
    sheet.save(sheet_path, quality=92)
    return masks, background_masks


def composite_attempt(source_views, donor_views, output_attempt, config):
    source_views = Path(source_views).resolve()
    donor_views = Path(donor_views).resolve()
    output_attempt = Path(output_attempt).resolve()
    masks, background_masks = prepare_masks(source_views, output_attempt, config)
    background_only = config.get('backgroundOnly', False)
    views = {}
    for slot in SLOTS:
        source_path = source_views / 'raw' / f'{slot}.png'
        donor_path = source_path if background_only else donor_views / 'raw' / f'{slot}.png'
        if not donor_path.is_file():
            raise RuntimeError(f'Donor view is missing: {donor_path}')
        output_path = output_attempt / 'composite' / f'{slot}.png'
        views[slot] = composite_view(source_path, donor_path, masks[slot], output_path, background_masks.get(slot))
        views[slot]['sourceSha256'] = __import__('hashlib').sha256(source_path.read_bytes()).hexdigest()
        views[slot]['donorSha256'] = __import__('hashlib').sha256(donor_path.read_bytes()).hexdigest()
        views[slot]['maskSha256'] = __import__('hashlib').sha256((output_attempt / 'masks' / f'{slot}.png').read_bytes()).hexdigest()
        views[slot]['compositeSha256'] = __import__('hashlib').sha256(output_path.read_bytes()).hexdigest()
    report = {
        'version': 1,
        'createdAt': datetime.now(timezone.utc).isoformat(),
        'sourceViews': str(source_views),
        'donorViews': None if background_only else str(donor_views),
        'backgroundOnly': background_only,
        'featherPx': config.get('featherPx', 0),
        'backgroundFeatherPx': config.get('backgroundFeatherPx', config.get('featherPx', 0)),
        'rectangles': config['masks'],
        'backgroundRectangles': config.get('backgroundMasks', {}),
        'views': views,
    }
    output_attempt.mkdir(parents=True, exist_ok=True)
    (output_attempt / 'composition.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'composite'))
    parser.add_argument('--source', required=True)
    parser.add_argument('--donor')
    parser.add_argument('--output', required=True)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == 'prepare':
        prepare_masks(args.source, args.output, config)
        print(json.dumps({'maskPreview': str(Path(args.output).resolve() / 'mask-preview.jpg')}, indent=2))
    else:
        if not args.donor:
            parser.error('--donor is required for composite')
        print(json.dumps(composite_attempt(args.source, args.donor, args.output, config), indent=2))


if __name__ == '__main__':
    main()

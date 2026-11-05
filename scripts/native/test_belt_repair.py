import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image


MODULE_PATH = Path(__file__).with_name('belt_repair.py')


def load_module():
    spec = importlib.util.spec_from_file_location('belt_repair', MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError('belt_repair.py is missing')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BeltRepairTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.config = {
            'version': 1,
            'featherPx': 0,
            'masks': {
                'front': [[0.25, 0.25, 0.75, 0.75]],
                'left': [[0.25, 0.25, 0.75, 0.75]],
                'back': [[0.25, 0.25, 0.75, 0.75]],
                'right': [[0.25, 0.25, 0.75, 0.75]],
            },
        }

    def test_normalized_rectangles_build_expected_mask(self):
        mask = self.module.rectangle_mask((100, 80), self.config['masks']['front'])
        self.assertEqual(mask.mode, 'L')
        self.assertEqual(mask.getpixel((24, 20)), 0)
        self.assertEqual(mask.getpixel((25, 20)), 255)
        self.assertEqual(mask.getpixel((74, 59)), 255)
        self.assertEqual(mask.getpixel((75, 60)), 0)

    def test_composite_copies_donor_only_inside_mask(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.png'
            donor = root / 'donor.png'
            output = root / 'output.png'
            Image.new('RGB', (100, 80), (10, 20, 30)).save(source)
            Image.new('RGB', (100, 80), (200, 210, 220)).save(donor)
            mask = self.module.rectangle_mask((100, 80), self.config['masks']['front'])
            report = self.module.composite_view(source, donor, mask, output)
            result = Image.open(output).convert('RGB')
            self.assertEqual(result.getpixel((0, 0)), (10, 20, 30))
            self.assertEqual(result.getpixel((50, 40)), (200, 210, 220))
            self.assertEqual(report['outsideChangedPixels'], 0)
            self.assertEqual(report['insideChangedPixels'], 2000)

    def test_background_cleanup_erases_only_its_own_mask(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.png'
            donor = root / 'donor.png'
            output = root / 'output.png'
            Image.new('RGB', (100, 80), (10, 20, 30)).save(source)
            Image.new('RGB', (100, 80), (200, 210, 220)).save(donor)
            donor_mask = self.module.rectangle_mask((100, 80), self.config['masks']['front'])
            background_mask = self.module.rectangle_mask((100, 80), [[0.4, 0.4, 0.6, 0.6]])
            report = self.module.composite_view(source, donor, donor_mask, output, background_mask)
            result = Image.open(output).convert('RGB')
            self.assertEqual(result.getpixel((0, 0)), (10, 20, 30))
            self.assertEqual(result.getpixel((30, 30)), (200, 210, 220))
            self.assertEqual(result.getpixel((50, 40)), (10, 20, 30))
            self.assertEqual(report['outsideChangedPixels'], 0)
            self.assertEqual(report['backgroundMaskPixels'], 320)

    def test_composite_rejects_mismatched_dimensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.png'
            donor = root / 'donor.png'
            Image.new('RGB', (100, 80)).save(source)
            Image.new('RGB', (101, 80)).save(donor)
            mask = Image.new('L', (100, 80), 255)
            with self.assertRaisesRegex(RuntimeError, 'same dimensions'):
                self.module.composite_view(source, donor, mask, root / 'output.png')

    def test_invalid_or_empty_masks_are_rejected(self):
        for rectangles in ([], [[0.2, 0.2, 0.2, 0.8]], [[-0.1, 0.2, 0.8, 0.8]], [[0.1, 0.2, 1.1, 0.8]]):
            with self.subTest(rectangles=rectangles):
                with self.assertRaisesRegex(RuntimeError, 'mask'):
                    self.module.rectangle_mask((100, 80), rectangles)

    def test_composite_attempt_requires_every_view_and_writes_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            donor = root / 'donor'
            output = root / 'attempt'
            for slot in ('front', 'left', 'back', 'right'):
                for base, color in ((source, (10, 20, 30)), (donor, (200, 210, 220))):
                    directory = base / 'raw'
                    directory.mkdir(parents=True, exist_ok=True)
                    Image.new('RGB', (100, 80), color).save(directory / f'{slot}.png')
            self.config['featherPx'] = 8
            self.config['backgroundFeatherPx'] = 0
            self.config['backgroundMasks'] = {'left': [[0.4, 0.4, 0.6, 0.6]]}
            report = self.module.composite_attempt(source, donor, output, self.config)
            self.assertEqual(sorted(report['views']), ['back', 'front', 'left', 'right'])
            self.assertEqual(report['backgroundRectangles'], self.config['backgroundMasks'])
            self.assertEqual(report['backgroundFeatherPx'], 0)
            self.assertEqual(Image.open(output / 'masks' / 'background-left.png').getpixel((40, 32)), 255)
            self.assertEqual(Image.open(output / 'composite' / 'left.png').convert('RGB').getpixel((50, 40)), (10, 20, 30))
            self.assertEqual(Image.open(output / 'composite' / 'front.png').convert('RGB').getpixel((0, 0)), (10, 20, 30))
            self.assertTrue((output / 'composite' / 'front.png').is_file())
            self.assertTrue((output / 'masks' / 'front.png').is_file())
            self.assertTrue((output / 'masks' / 'background-left.png').is_file())
            self.assertTrue((output / 'mask-preview.jpg').is_file())
            self.assertTrue((output / 'composition.json').is_file())
            (donor / 'raw' / 'right.png').unlink()
            with self.assertRaisesRegex(RuntimeError, 'right.png'):
                self.module.composite_attempt(source, donor, root / 'second', self.config)

    def test_background_only_never_reads_or_reapplies_donor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            donor = root / 'empty-donor'
            output = root / 'attempt'
            for slot in ('front', 'left', 'back', 'right'):
                directory = source / 'raw'
                directory.mkdir(parents=True, exist_ok=True)
                Image.new('RGB', (100, 80), (10, 20, 30)).save(directory / f'{slot}.png')
            self.config['backgroundOnly'] = True
            self.config['backgroundMasks'] = {'left': [[0.4, 0.4, 0.6, 0.6]]}
            report = self.module.composite_attempt(source, donor, output, self.config)
            self.assertTrue(report['backgroundOnly'])
            self.assertEqual(Image.open(output / 'composite' / 'front.png').convert('RGB').getpixel((50, 40)), (10, 20, 30))
            self.assertEqual(report['views']['front']['insideChangedPixels'], 0)
            self.assertEqual(Image.open(output / 'composite' / 'left.png').convert('RGB').getpixel((50, 40)), (10, 20, 30))


if __name__ == '__main__':
    unittest.main()

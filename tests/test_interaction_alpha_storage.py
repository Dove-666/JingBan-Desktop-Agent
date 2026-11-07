"""Portable alpha-storage contracts over one owned synthetic interaction rig."""
import json
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.part_interaction import PartInteraction
from tests.test_part_interaction import make_part_fixture


def alpha_bytes(image):
    image=image.convertToFormat(QImage.Format.Format_Alpha8)
    raw=image.constBits();stride=image.bytesPerLine()
    return b''.join(bytes(raw[y*stride:y*stride+image.width()]) for y in range(image.height()))


def resident_bytes(bank):
    images={}
    for rig in {id(rig):rig for rig in bank.rigs.values()}.values():
        for image in [rig.source,rig.base,rig.cutout_mask,*[part.image for part in rig.parts],
            *[part.hit_mask for part in rig.parts if part.hit_mask is not None],*[mask for _,mask in rig.repair_masks.values()]]:
            images.setdefault(image.cacheKey(),image.sizeInBytes())
    return sum(images.values())


class InteractionAlphaStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='aura-alpha-storage-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        rig_path, source_path, data = make_part_fixture(self.root)
        self.source_path = source_path
        original = QImage(str(self.root / data['cutout_mask']))
        for index, opacity in enumerate((0, 1, 63, 127, 128, 254, 255)):
            original.setPixelColor(350 + index, 400, QColor(31, 79, 143, opacity))
        data['cutout_mask'] = 'owned-soft-color-alpha.png'
        self.assertTrue(original.save(str(self.root / data['cutout_mask'])))
        for index, item in enumerate(data['parts']):
            full_canvas = item['name'] == 'left_ear'
            size = QSize(1024, 1024) if full_canvas else QImage(str(self.root / item['image'])).size()
            mask = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
            mask.fill(Qt.GlobalColor.transparent)
            if item['name'] != 'tail':
                rect = QRect(250, 350, 13, 11) if full_canvas else QRect(33, 19, 9, 7)
                for y in range(rect.top(), rect.bottom() + 1):
                    for x in range(rect.left(), rect.right() + 1):
                        opacity = (1, 63, 127, 128, 254, 255)[(x + y) % 6]
                        mask.setPixelColor(x, y, QColor(17, 91, 203, opacity))
            item['hit_mask'] = f'owned-click-region-{index}.png'
            self.assertTrue(mask.save(str(self.root / item['hit_mask'])))
        rig_path.write_text(json.dumps(data), encoding='utf8')
        self.metadata = data
        self.bank = PartInteraction(rig_path, source_path)
        self.assertIsNotNone(self.bank.rig)
        self.rig = self.bank.rig

    def masks(self):
        return [self.rig.cutout_mask, *[mask for _, mask in self.rig.repair_masks.values()],
                *[part.hit_mask for part in self.rig.parts if part.hit_mask is not None]]

    def test_owned_rig_uses_alpha8_only_for_stored_pure_masks(self):
        for image in self.masks():
            self.assertEqual(image.format(), QImage.Format.Format_Alpha8)
        for image in [self.rig.source, self.rig.base, *[part.image for part in self.rig.parts]]:
            self.assertEqual(image.format(), QImage.Format.Format_ARGB32_Premultiplied)

    def test_owned_mask_bytes_follow_alpha8_alignment_without_a_fixed_bank_budget(self):
        masks = self.masks()
        for image in masks:
            self.assertGreaterEqual(image.bytesPerLine(), image.width())
            self.assertLessEqual(image.bytesPerLine(), image.width() + 3)
            self.assertEqual(image.sizeInBytes(), image.bytesPerLine() * image.height())
        self.assertLessEqual(sum(image.sizeInBytes() for image in masks),
                            sum((image.width() + 3) * image.height() for image in masks))
        parts = {part.name: part for part in self.rig.parts}
        self.assertEqual(parts['left_ear'].hit_mask.size(), QSize(13, 11))
        self.assertEqual(parts['left_ear'].hit_mask.bytesPerLine(), 16)
        self.assertEqual(parts['right_ear'].hit_mask.size(), QSize(9, 7))
        self.assertEqual(parts['right_ear'].hit_mask.bytesPerLine(), 12)
        self.assertEqual(parts['tail'].hit_mask.size(), QSize(1, 1))
        self.assertEqual(alpha_bytes(parts['tail'].hit_mask), b'\0')

    def test_regions_match_metadata_png_alpha_and_visible_layers_keep_colors(self):
        original = QImage(str(self.root / self.metadata['cutout_mask']))
        self.assertEqual(alpha_bytes(original), alpha_bytes(self.rig.cutout_mask))
        for point, image in self.rig.repair_masks.values():
            self.assertEqual(alpha_bytes(original.copy(QRect(point, image.size()))), alpha_bytes(image))
        by_name = {item['name']: item for item in self.metadata['parts']}
        for part in self.rig.parts:
            spec = by_name[part.name]
            original_hit = QImage(str(self.root / spec['hit_mask']))
            relative = part.hit_origin if original_hit.size() == self.rig.canvas else part.hit_origin - part.origin
            expected = original_hit.copy(QRect(round(relative.x()), round(relative.y()),
                                              part.hit_mask.width(), part.hit_mask.height()))
            self.assertEqual(alpha_bytes(expected), alpha_bytes(part.hit_mask))
            colored = QImage(str(self.root / spec['image'])).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            self.assertEqual(part.image, colored)
        colored_base = QImage(str(self.root / self.metadata['base'])).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        self.assertEqual(self.rig.base, colored_base)
        colored_source = QImage(str(self.source_path)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        self.assertEqual(self.rig.source, colored_source)


if __name__=='__main__':unittest.main()

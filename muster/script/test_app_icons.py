"""The generated icon assets are complete and shaped like a Mac app icon.

Standard library only, so this runs on any CI runner.  `generate_app_icons.py` needs Pillow
and writes the assets;  this reads what it wrote.
"""

import json
import pathlib
import struct
import unittest
import zlib

MUSTER = pathlib.Path(__file__).resolve().parent.parent
CATALOG = MUSTER / "Muster" / "Assets.xcassets"
RGBA = 6
MAC_ICON_SLOTS = {
    ("16x16", "1x"): 16,
    ("16x16", "2x"): 32,
    ("32x32", "1x"): 32,
    ("32x32", "2x"): 64,
    ("128x128", "1x"): 128,
    ("128x128", "2x"): 256,
    ("256x256", "1x"): 256,
    ("256x256", "2x"): 512,
    ("512x512", "1x"): 512,
    ("512x512", "2x"): 1024,
}


def read_png(path):
    """Return (width, height, color type, first scanline without its filter byte)."""
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("%s is not a PNG" % path)
    offset = 8
    header = None
    compressed = b""
    while offset < len(data):
        length, kind = struct.unpack(">I4s", data[offset : offset + 8])
        body = data[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            compressed += body
        offset += length + 12
    width, height, _depth, color_type = header
    row_bytes = width * 4 if color_type == RGBA else width
    return width, height, color_type, zlib.decompress(compressed)[1 : row_bytes + 1]


class AppIconAssetTests(unittest.TestCase):
    def assert_is_shaped_icon(self, path, size):
        width, height, color_type, top_row = read_png(path)
        self.assertEqual((width, height), (size, size), path.name)
        self.assertEqual(color_type, RGBA, path.name + " has no alpha")
        # All zero whatever the row's filter:  the margin above the rounded square is empty.
        # Artwork left as a full square would paint the Dock a square tile.  Below 64 pixels
        # the margin is a few pixels and resampling leaves a faint trace in it.
        if size >= 64:
            self.assertEqual(top_row, bytes(len(top_row)), path.name + " is not inset")

    def test_app_icon_fills_every_mac_slot_with_the_right_size(self):
        folder = CATALOG / "AppIcon.appiconset"
        images = json.loads((folder / "Contents.json").read_text())["images"]
        slots = {(image["size"], image["scale"]): image for image in images}
        self.assertEqual(set(slots), set(MAC_ICON_SLOTS))
        for slot, pixels in MAC_ICON_SLOTS.items():
            self.assertEqual(slots[slot]["idiom"], "mac")
            self.assert_is_shaped_icon(folder / slots[slot]["filename"], pixels)

    def test_both_modes_have_their_own_runtime_image(self):
        bytes_by_name = {}
        for name in ("MusterIconLight", "MusterIconDark"):
            folder = CATALOG / (name + ".imageset")
            images = json.loads((folder / "Contents.json").read_text())["images"]
            self.assertEqual(len(images), 1, name)
            path = folder / images[0]["filename"]
            self.assert_is_shaped_icon(path, 1024)
            bytes_by_name[name] = path.read_bytes()
        self.assertNotEqual(bytes_by_name["MusterIconLight"], bytes_by_name["MusterIconDark"])

    def test_the_swift_names_match_the_image_sets(self):
        swift = (MUSTER / "Muster" / "Model" / "ThemeChoice.swift").read_text()
        for name in ("MusterIconLight", "MusterIconDark"):
            self.assertIn('"%s"' % name, swift)

    def test_the_project_points_the_bundle_at_the_app_icon(self):
        project = (MUSTER / "project.yml").read_text()
        self.assertIn("ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon", project)


if __name__ == "__main__":
    unittest.main()

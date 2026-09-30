"""Parser checks for the SFX extractors (no retail data)."""
import struct
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.asset_pipeline import audio_sfx as sfx


def make_abk():
    data = bytearray(0x400)
    data[:4] = b"ABKC"
    struct.pack_into(">H", data, 0x0A, 1)
    struct.pack_into(">I", data, 0x1C, 0x5C)
    struct.pack_into(">I", data, 0x20, 0x200)
    # One module, one player pointing at a sample table with two sounds.
    data[0x5C + 0x24] = 1
    struct.pack_into(">I", data, 0x5C + 0x2C, 0x100)
    struct.pack_into(">I", data, 0x5C + 0x3C, 0x3C)
    struct.pack_into(">I", data, 0x88, 0x100)
    struct.pack_into(">I", data, 0x140, 0x160)
    struct.pack_into(">I", data, 0x160, 2)
    struct.pack_into(">H", data, 0x164, 0)
    struct.pack_into(">I", data, 0x164 + 8, 0xFFFFFFFF)
    struct.pack_into(">H", data, 0x170, 1)
    struct.pack_into(">I", data, 0x170 + 8, 0xFFFFFFFF)
    # S10A bank with two files.
    data[0x200:0x204] = b"S10A"
    struct.pack_into(">I", data, 0x208, 2)
    struct.pack_into(">I", data, 0x20C, 0x40)
    struct.pack_into(">I", data, 0x210, 0x80)
    return bytes(data)


def make_blocks(sections):
    blob = bytearray()
    for chunk in sections:
        body = b"".join(struct.pack(">I", 4 * (len(part) + 4)) + part for part in chunk)
        blob += struct.pack(">I", 8 + len(body)) + struct.pack(">I", 512) + body
    return bytes(blob)


class SfxParserTests(unittest.TestCase):
    def test_abk_index_lists_ram_sounds(self):
        data, sounds = sfx.parse_abk_data(make_abk(), "synthetic.abk")
        self.assertEqual(len(sounds), 2)
        self.assertTrue(all(not s["streamed"] for s in sounds))
        self.assertEqual(sounds[0]["snr_off"], 0x200 + 0x40)
        self.assertEqual(sounds[1]["snr_off"], 0x200 + 0x80)
        self.assertLess(sounds[0]["snr_off"], sounds[0]["end"])

    def test_abk_rejects_magic(self):
        with self.assertRaises(ValueError):
            sfx.parse_abk_data(b"NOPE" + bytes(64), "x.abk")

    def test_layered_deblock_splits_streams(self):
        blob = make_blocks([[b"A" * 100, b"B" * 200, b"C" * 50],
                            [b"D" * 100, b"E" * 200, b"F" * 50]])
        layers = sfx.deblock_eaxma_layers(blob)
        self.assertEqual(len(layers), 3)
        pad100 = b"\xff" * (2048 - 100)
        pad50 = b"\xff" * (2048 - 50)
        self.assertEqual(layers[0], b"A" * 100 + pad100 + b"D" * 100)
        self.assertEqual(layers[2], b"C" * 50 + pad50 + b"F" * 50)

    def test_ram_candidates_try_wide_header_first(self):
        heads = [payload for _, payload in sfx.ram_candidates(b"\x00" * 64, 8, 64)]
        self.assertEqual([len(h) for h in heads], [64 - 28, 64 - 20, 64 - 24, 64 - 32, 64 - 16])

    def test_splc_index_slices_sounds(self):
        import struct
        sounds = [{"index": 0}, {"index": 1}]
        data = bytearray(0x200)
        data[:4] = b"SPLC"
        struct.pack_into(">6I", data, 4, 3, 0x80, 0, 0, 0, 2)
        for i, off in enumerate((0x100, 0x140)):
            struct.pack_into(">3I", data, 0x80 + 60 + i * 12, off, off + 0x40, i)
        blob = bytes(data)
        parsed = sfx.parse_splc(blob)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["start"], 0x80 + 60 + 24 + 0x100)
        self.assertEqual(parsed[0]["end"], 0x80 + 60 + 24 + 0x140)
        self.assertEqual(parsed[1]["end"], len(blob))

    def test_grain_splits_pattern_table(self):
        import struct
        data = bytearray(0x100)
        struct.pack_into(">I", data, 0, 0x40)
        grain = sfx.parse_grain(bytes(data))
        self.assertEqual(grain["snr_off"], 0x40)
        self.assertEqual(len(grain["pattern"]), 0x40 - 0x24)


if __name__ == "__main__":
    unittest.main()

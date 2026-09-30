"""Pairing checks for the speech extractor (no retail data)."""
import struct
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.asset_pipeline import audio_speech as speech


def make_line(num_sounds=2, stride=4):
    hdr = bytearray(0x10 + stride * num_sounds)
    hdr[2] = (stride - 2) & 0x7F
    hdr[3] = num_sounds
    for n in range(num_sounds):
        struct.pack_into(">H", hdr, 0x10 + stride * n, 12 * n)
    sth = bytearray()
    for n in range(num_sounds):
        sth += struct.pack(">I", 0x100 * (n + 1))
        sth += bytes.fromhex("0300bb800007d000")
    dat = bytes(0x500)
    return bytes(hdr), bytes(sth), dat


class SpeechPairingTests(unittest.TestCase):
    def test_sounds_span_to_next_offset(self):
        hdr, sth, dat = make_line()
        sounds = list(speech.iter_sounds(hdr, sth, dat))
        self.assertEqual(len(sounds), 2)
        self.assertEqual(sounds[0][0], 1)
        self.assertEqual(len(sounds[0][2]), 0x100)
        self.assertEqual(len(sounds[1][2]), len(dat) - 0x200)

    def test_snr_headers_read(self):
        from tools.asset_pipeline.audio_music import parse_snr
        hdr, sth, dat = make_line()
        for _, info, _ in speech.iter_sounds(hdr, sth, dat):
            self.assertEqual(info["channels"], 1)
            self.assertEqual(info["rate"], 48000)

    def test_missing_archives_skip(self):
        import io
        import tempfile
        log = io.StringIO()
        out = Path(tempfile.mkdtemp()) / "audio"
        man = speech.convert_speech(Path(tempfile.mkdtemp()), out,
                                    Path(tempfile.mkdtemp()),
                                    lambda _: None, log, "ffmpeg")
        self.assertEqual(man["groups"], {})
        self.assertIn("archive missing", log.getvalue())


if __name__ == "__main__":
    unittest.main()

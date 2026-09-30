"""Parser and packaging checks for the audio asset group (no retail data)."""
import json
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.asset_pipeline import audio_music as music
from tools.asset_pipeline import versions

def make_mpf():
    data = bytearray(0x100)
    data[:4] = b"PFDx"
    data[4] = 5
    data[5] = 3
    data[0x0D] = 1
    # tracks table at 0x40 holds one entry pointing at 0x60 (*4 applied).
    struct.pack_into(">I", data, 0x2C, 0x40)
    struct.pack_into(">I", data, 0x34, 0x70)
    struct.pack_into(">I", data, 0x38, 0x78)
    struct.pack_into(">I", data, 0x40, 0x18)
    struct.pack_into(">I", data, 0x60, 0)
    struct.pack_into(">H", data, 0x64, 0)
    struct.pack_into(">2I", data, 0x70, 0xAAE07D49, 47197)
    return bytes(data)

def make_mus(sounds=2):
    header = struct.pack("<2I", 0xAAE07D49, sounds)
    header += bytes(0x28 - len(header))
    body = bytearray()
    for i in range(sounds):
        body += struct.pack(">IHHIIIII", 0x11223344 + i, i, 0,
                            0x100 + i, 0x200 + i, 8, 0x1000, 0)
    return header + bytes(body)

def make_snr(channels=2, rate=44100, samples=70560):
    h1 = (0 << 28) | (3 << 24) | ((channels - 1) << 18) | rate
    h2 = (1 << 30) | samples
    return struct.pack(">2I", h1, h2)

class MusicParserTests(unittest.TestCase):
    def test_mpf_tables(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "game.mpf"
            path.write_bytes(make_mpf())
            result = music.parse_mpf(path)
        self.assertEqual(len(result["tracks"]), 1)
        self.assertEqual(result["tracks"][0]["start"], 0)
        self.assertEqual(len(result["streams"]), 1)
        self.assertEqual(result["streams"][0]["duration_ms"], 47197)

    def test_mus_records_sector_scaled(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Game_Stream.mus"
            path.write_bytes(make_mus())
            _, sounds = music.parse_mus(path)
        self.assertEqual(len(sounds), 2)
        self.assertEqual(sounds[0]["snr_off"], 0x100 * 0x10)
        self.assertEqual(sounds[0]["sns_off"], 0x200 * 0x80)
        self.assertEqual(sounds[0]["sns_size"], 0x1000)
        self.assertEqual(sounds[1]["index"], 1)

    def test_snr_bitfields(self):
        info = music.parse_snr(make_snr())
        self.assertEqual(info, {"version": 0, "codec": 3, "channels": 2,
                                "rate": 44100, "type": 1, "loop": 0,
                                "samples": 70560})

    def test_riff_wrapper_tags_xma2(self):
        blob = music.riff_xma2(b"\x00" * 2048, 2, 44100, 70560)
        self.assertTrue(blob.startswith(b"RIFF") and b"WAVEfmt " in blob[:32])
        tag, channels, rate = struct.unpack("<HHI", blob[20:28])
        self.assertEqual((tag, channels, rate), (0x166, 2, 44100))
        self.assertIn(b"data", blob[20:96])

    def test_bad_playlist_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.mpf"
            path.write_bytes(b"NOPE" + bytes(64))
            with self.assertRaises(ValueError):
                music.parse_mpf(path)

    def test_audio_group_versioned(self):
        prints = versions.fingerprints()
        self.assertIn("audio", prints)
        self.assertEqual(len(prints["audio"]), 64)
        # Legacy markers without an audio pipeline trigger one refresh.
        self.assertIn("audio", versions.changed_groups({}, prints))

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "needs FFmpeg")
    def test_verify_duration_accepts_matching_output(self):
        import shutil
        import subprocess
        import tempfile
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / "tone.wav"
            subprocess.run([ffmpeg, "-hide_banner", "-y", "-v", "error",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                            "-ar", "44100", str(src)],
                           check=True, capture_output=True)
            out = Path(temp) / "tone.ogg"
            subprocess.run([ffmpeg, "-hide_banner", "-y", "-v", "error",
                            "-i", str(src), "-c:a", "libvorbis", str(out)],
                           check=True, capture_output=True)
            from tools.asset_pipeline.audio_ffmpeg import probe_for
            ffprobe = probe_for(ffmpeg)
            if ffprobe is None:
                self.skipTest("needs ffprobe")
            music.verify_duration(ffprobe, out, {"samples": 44100, "rate": 44100})
            with self.assertRaises(RuntimeError):
                music.verify_duration(ffprobe, out, {"samples": 44100 * 10, "rate": 44100})

if __name__ == "__main__":
    unittest.main()

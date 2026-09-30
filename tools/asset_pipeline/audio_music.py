"""EA music (MPF+MUS, Xbox 360 XMA2) extraction for the audio asset group.
Reverse-engineered layout (validated against vgmstream's EA parsers and its
decode output; only format facts are used here, no third-party code):
- MPF v5.3 ("PFDx", big-endian): track table at u32be(0x2c), sample table at
  u32be(0x34), EOF at u32be(0x38); 8-byte sample records (mus_checksum,
  duration_ms). Track entries map ordered streams for the in-game sequencer.
- MUS v5.3: u32le sound count at 0x04, 0x1c-byte records at 0x28 holding
  SNR/SNS offsets in 0x10/0x80-byte sectors plus sizes. Each stream is an
  EA SNR header (8-byte bit-packed BE: version/codec/channels/rate/samples)
  plus XMA2 packets, decoded via an XMA2 RIFF wrapper + FFmpeg.
"""
import json
import os
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

STATIONS = (
    ("game", "game.mpf", "Game_Stream.mus"),
    ("world", "world.mpf", "World_Stream.mus"),
    ("ipod", "ipod.mpf", "Ipod_Stream.mus"),
)


def parse_mpf(path):
    data = Path(path).read_bytes()
    if data[:4] != b"PFDx" or data[4] != 5:
        raise ValueError(f"Unsupported playlist: {path}")
    num_tracks = data[0x0D]
    tracks_table = struct.unpack(">I", data[0x2C:0x30])[0]
    samples_table = struct.unpack(">I", data[0x34:0x38])[0]
    eof = struct.unpack(">I", data[0x38:0x3C])[0]
    total = (eof - samples_table) // 8
    samples = []
    for s in range(total):
        checksum, duration = struct.unpack(">2I", data[samples_table + s * 8:][:8])
        samples.append({"checksum": checksum, "duration_ms": duration})
    tracks = []
    for i in range(num_tracks):
        entry = struct.unpack(">I", data[tracks_table + i * 4:][:4])[0] * 4
        start = struct.unpack(">I", data[entry:entry + 4])[0]
        nsub = struct.unpack(">H", data[entry + 4:entry + 6])[0]
        tracks.append({"start": start, "subbanks": nsub})
    return {"tracks": tracks, "streams": samples}


def parse_mus(path):
    data = Path(path).read_bytes()
    count = struct.unpack("<I", data[4:8])[0]
    sounds = []
    for i in range(count):
        rec = data[0x28 + i * 0x1C:0x28 + (i + 1) * 0x1C]
        checksum, index, sub, snr, sns, snr_size, sns_size, _ = struct.unpack(">IHHIIIII", rec)
        sounds.append({"checksum": checksum, "index": index, "sub": sub,
                       "snr_off": snr * 0x10, "sns_off": sns * 0x80,
                       "snr_size": snr_size, "sns_size": sns_size})
    return data, sounds


def parse_snr(data):
    h1, h2 = struct.unpack(">2I", data[:8])
    return {"version": (h1 >> 28) & 0xF, "codec": (h1 >> 24) & 0xF,
            "channels": ((h1 >> 18) & 0x3F) + 1, "rate": h1 & 0x3FFFF,
            "type": (h2 >> 30) & 0x3, "loop": (h2 >> 29) & 0x1,
            "samples": h2 & 0x1FFFFFFF}


def riff_xma2(payload, channels, rate, samples):
    mask = {1: 0x4, 2: 0x3}.get(channels, 0)
    extra = struct.pack("<HIIIIIII BH", 1, mask, samples, 2048, 0, samples, 0, 0, 0, 4)
    extra += b"\x00" * (34 - len(extra))
    fmt = struct.pack("<HHIIHHH", 0x166, channels, rate, 0, 2048, 16, len(extra)) + extra
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(payload)) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(payload)) + payload)


def deblock_eaxma(data):
    """Strip EA-XMA block headers, returning raw XMA packets.

    Each block is [flag+size u32be][num-samples u32be][stream sections...];
    a section is [size*4 u32be][payload]. Payloads are padded with 0xFF to a
    0x800 multiple, matching the logical stream decoders consume.
    """
    out = bytearray()
    pos = 0
    size = len(data)
    while pos + 8 <= size:
        flag = data[pos]
        block = int.from_bytes(data[pos:pos + 4], "big") & 0xFFFFFF
        if block == 0 or (flag != 0x00 and flag != 0x80):
            break
        skip = 0x04 + 0x04
        section = int.from_bytes(data[pos + skip:pos + skip + 4], "big") // 4
        skip += 0x04
        section -= 0x04
        if section <= 0 or pos + skip + section > size:
            break
        out += data[pos + skip:pos + skip + section]
        pad = (-len(out)) % 0x800
        # Only pad mid-stream; the final block keeps its exact tail.
        if pos + block < size and pad:
            out += b"\xff" * pad
        pos += block
    return bytes(out)


def decode_stream(mus_data, sound, ffmpeg, out_ogg):
    snr = mus_data[sound["snr_off"]:sound["snr_off"] + sound["snr_size"]]
    info = parse_snr(snr)
    if info["version"] != 0 or info["codec"] != 3:
        raise ValueError(f"Unsupported EA stream: {info}")
    sns = mus_data[sound["sns_off"]:sound["sns_off"] + sound["sns_size"]]
    payload = deblock_eaxma(sns) if info["type"] == 1 else sns
    if not payload:
        raise ValueError("Empty XMA payload after deblocking")
    riff = riff_xma2(payload, info["channels"], info["rate"], info["samples"])
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-y", "-v", "error",
         "-i", "pipe:0", "-map", "0:a", "-c:a", "libvorbis", "-q:a", "5",
         str(out_ogg)],
        input=riff, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg decode failed: {proc.stderr.decode(errors='replace')[:500]}")
    info["duration_s"] = info["samples"] / info["rate"]
    return info


def convert(game_root, out_dir, report, log, ffmpeg, workers=None):
    """Convert every station; returns the music manifest dict."""
    game_root, out_dir = Path(game_root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    workers = workers or min(8, max(1, (os.cpu_count() or 2) // 2))
    manifest = {"version": 1, "stations": {}}
    for station, mpf_name, mus_name in STATIONS:
        report(f"Converting music station: {station}")
        mpf = parse_mpf(game_root / "data/audio/music" / mpf_name)
        log.write(f"{station}: playlist has {len(mpf['tracks'])} tracks, "
                  f"{len(mpf['streams'])} sequenced streams\n")
        mus_data, sounds = parse_mus(game_root / "data/audio/music" / mus_name)
        station_dir = out_dir / station
        station_dir.mkdir(exist_ok=True)
        tracks = [None] * len(sounds)

        def job(i):
            sound = sounds[i]
            target = station_dir / f"{i:04d}.ogg"
            try:
                info = decode_stream(mus_data, sound, ffmpeg, target)
            except (ValueError, RuntimeError) as error:
                return (i, None, f"{station} stream {i}: skipped ({error})")
            return (i, {"file": f"{station}/{target.name}", "index": i,
                        "samples": info["samples"], "rate": info["rate"],
                        "channels": info["channels"],
                        "duration_s": round(info["duration_s"], 3)}, None)

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(job, i) for i in range(len(sounds))]
            for future in as_completed(futures):
                i, track, warning = future.result()
                if warning is not None:
                    log.write(warning + "\n")
                else:
                    tracks[i] = track
                done += 1
                if done % 500 == 0:
                    report(f"{station}: {done}/{len(sounds)} streams")
        tracks = [t for t in tracks if t is not None]
        manifest["stations"][station] = {"tracks": tracks, "count": len(tracks)}
        report(f"{station}: {len(tracks)}/{len(sounds)} streams converted")
    (out_dir / "music.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest

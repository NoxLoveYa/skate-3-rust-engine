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


def deblock_eaxma_layers(data):
    """One raw XMA payload per layer stream (1/2ch each, in file order)."""
    blocks = []
    pos = 0
    size = len(data)
    while pos + 8 <= size:
        flag = data[pos]
        block = int.from_bytes(data[pos:pos + 4], "big") & 0xFFFFFF
        if block == 0 or (flag != 0x00 and flag != 0x80):
            break
        sections = []
        cursor = pos + 0x04 + 0x04
        while cursor + 4 <= pos + block:
            section = int.from_bytes(data[cursor:cursor + 4], "big") // 4
            cursor += 0x04
            section -= 0x04
            if section <= 0 or cursor + section > pos + block:
                break
            sections.append(data[cursor:cursor + section])
            cursor += section
        if sections:
            blocks.append(sections)
        pos += block
    layers: list[bytearray] = []
    last = len(blocks) - 1
    for b, sections in enumerate(blocks):
        while len(layers) < len(sections):
            layers.append(bytearray())
        for payload, layer in zip(sections, layers):
            layer += payload
            if b != last:
                pad = (-len(layer)) % 0x800
                if pad:
                    layer += b"\xff" * pad
    return [bytes(layer) for layer in layers]

def decode_stream(mus_data, sound, ffmpeg, out_ogg):
    snr = mus_data[sound["snr_off"]:sound["snr_off"] + sound["snr_size"]]
    info = parse_snr(snr)
    if info["version"] != 0 or info["codec"] != 3:
        raise ValueError(f"Unsupported EA stream: {info}")
    sns = mus_data[sound["sns_off"]:sound["sns_off"] + sound["sns_size"]]
    # STREAM payloads are EA-blocked; anything else arrives as raw packets.
    payload = deblock_eaxma(sns) if info["type"] == 1 else sns
    if not payload:
        raise ValueError("Empty XMA payload after deblocking")
    if info["channels"] not in (1, 2):
        raise ValueError(f"Multichannel layers need per-layer streams: {info}")
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

def decode_layered(layers, channels, rate, samples, ffmpeg, out_ogg, work):
    """Decode 1/2ch XMA layers and merge them to one multichannel file."""
    work.mkdir(parents=True, exist_ok=True)
    counts = [2] * (channels // 2) + ([1] if channels % 2 else [])
    if len(layers) != len(counts):
        raise ValueError(f"Expected {len(counts)} XMA layers, found {len(layers)}")
    wavs = []
    for n, (payload, count) in enumerate(zip(layers, counts)):
        target = work / f"layer{n}.wav"
        riff = riff_xma2(payload, count, rate, samples)
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-y", "-v", "error",
             "-i", "pipe:0", "-map", "0:a", "-c:a", "pcm_s16le", str(target)],
            input=riff, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg layer decode failed: {proc.stderr.decode(errors='replace')[:300]}")
        wavs.append(target)
    inputs = []
    for wav in wavs:
        inputs += ["-i", str(wav)]
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-y", "-v", "error", *inputs,
         "-filter_complex", f"amerge=inputs={len(wavs)}",
         "-c:a", "libvorbis", "-q:a", "5", str(out_ogg)],
        capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg merge failed: {proc.stderr.decode(errors='replace')[:300]}")
    import shutil
    shutil.rmtree(work, ignore_errors=True)


def verify_duration(ffprobe, path, info):
    """Spot-check one output against its header sample count."""
    proc = subprocess.run(
        [str(ffprobe), "-hide_banner", "-v", "error", "-show_entries",
         "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[:200]}")
    try:
        actual = float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe unreadable duration: {proc.stdout[:50]!r}")
    expected = info["samples"] / info["rate"]
    if abs(actual - expected) > 0.05:
        raise RuntimeError(f"duration {actual:.3f}s != header {expected:.3f}s")
    return actual

def convert(game_root, out_dir, report, log, ffmpeg, workers=None, verify_every=128):
    """Convert every station; returns the music manifest dict."""
    game_root, out_dir = Path(game_root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    workers = workers or min(8, max(1, (os.cpu_count() or 2) // 2))
    from .audio_ffmpeg import probe_for
    ffprobe = probe_for(ffmpeg)
    if ffprobe is None:
        log.write("ffprobe unavailable; skipping duration spot-checks\n")
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
                if ffprobe is not None and i % verify_every == 0:
                    verify_duration(ffprobe, target, info)
            except (ValueError, RuntimeError) as error:
                target.unlink(missing_ok=True)
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

"""EA SFX banks (ABK/S10A, Xbox 360 XMA2) extraction for the audio asset group.

Layout (reverse-engineered, validated against vgmstream's EA parsers and its
decode output; only format facts are used here, no third-party code):
- ABKC header (big-endian here): module count u16 at 0x0A, modules table at
  u32(0x1C), S10A bank at u32(0x20). Modules list players, players point at
  sample tables, table entries are 12 bytes (sound index u16, streamed data
  offset u32 at +8; sounds sharing a table are deduplicated).
- S10A bank: file count u32be at +8, per-file SNR offsets u32be at +0x0C
  (relative to the S10A start). A streamed offset of 0xFFFFFFFF marks a RAM
  asset whose XMA payload follows its SNR header in place.
- Standalone .sns ambience/rolling stock pairs with .snr descriptors by
  stem; STREAM payloads are EA-blocked, RAM payloads are contiguous.
  Multichannel beds decode per layer and merge to one file.
"""
import json
import os
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .audio_music import (
    deblock_eaxma, deblock_eaxma_layers, decode_layered, parse_snr, riff_xma2,
    verify_duration,
)
from tools.owned_game.big import BigArchive


def parse_abk(path):
    data = Path(path).read_bytes()
    return parse_abk_data(data, path)


def parse_abk_data(data, name):
    if data[:4] != b"ABKC":
        raise ValueError(f"Not an EA bank: {name}")
    endian = ">" if struct.unpack(">I", data[0x1C:0x20])[0] in (0x5C, 0x78) else "<"
    num_modules = struct.unpack(endian + "H", data[0x0A:0x0C])[0]
    modules_table = struct.unpack(endian + "I", data[0x1C:0x20])[0]
    bnk_offset = struct.unpack(endian + "I", data[0x20:0x24])[0]
    if data[bnk_offset:bnk_offset + 4] != b"S10A":
        raise ValueError(f"Missing S10A bank: {name}")
    if modules_table == 0x5C:
        players_off, module_off, entry_size, table_off = 0x24, 0x2C, 0x3C, 0x04
    elif modules_table == 0x78:
        players_off, module_off, entry_size, table_off = 0x40, 0x54, 0x68, 0x0C
    else:
        raise ValueError(f"Unknown ABK module table: {modules_table:#x}")
    seen = set()
    sounds = []
    table = modules_table
    for _ in range(num_modules):
        num_players = data[table + players_off]
        if num_players == 0xFF:
            raise ValueError("Truncated ABK module list")
        module_data = struct.unpack(endian + "I", data[table + module_off:][:4])[0]
        for j in range(num_players):
            player = struct.unpack(endian + "I", data[table + entry_size + 4 * j:][:4])[0]
            samples_table = struct.unpack(endian + "I", data[module_data + player + table_off:][:4])[0]
            if samples_table in seen:
                continue
            seen.add(samples_table)
            count = struct.unpack(endian + "I", data[samples_table:samples_table + 4])[0]
            if count == 0xFFFFFFFF:
                raise ValueError("Truncated ABK sample table")
            for k in range(count):
                entry = samples_table + 0x04 + 0x0C * k
                index = struct.unpack(endian + "H", data[entry:entry + 2])[0]
                if index == 0xFFFF:
                    continue
                offset = struct.unpack(endian + "I", data[entry + 8:entry + 12])[0]
                sounds.append({"index": index, "offset": offset,
                               "streamed": offset != 0xFFFFFFFF})
        num_players += data[table + players_off + 0x03]
        table += entry_size + num_players * 0x04
    num_files = struct.unpack(">I", data[bnk_offset + 8:bnk_offset + 12])[0]
    for sound in sounds:
        if sound["index"] >= num_files:
            raise ValueError(f"Sound index out of range: {sound}")
        sound["snr_off"] = bnk_offset + struct.unpack(
            ">I", data[bnk_offset + 0x0C + 4 * sound["index"]:][:4])[0]
    # RAM payloads run to the next header; bound each by its successor.
    ordered = sorted(sounds, key=lambda s: s["snr_off"])
    for sound, following in zip(ordered, ordered[1:] + [None]):
        sound["end"] = following["snr_off"] if following else len(data)
    return data, sounds


def ram_candidates(data, snr_off, end):
    """Plausible (head_size, payload) pairs for an in-place SNR header.

    The extension size varies (loop points and size fields); duration
    verification in the caller selects the working candidate.
    """
    for head_size in (20, 12, 16, 24, 8):
        yield head_size, data[snr_off + head_size:end]


def parse_splc(data):
    """Index an SPLC random-pool bank; sounds slice [start:next start]."""
    if data[:4] != b"SPLC":
        raise ValueError("Not an SPLC bank")
    _ver, diroff, _c1, _c2, _rsv, ns = struct.unpack(">6I", data[4:28])
    base = diroff + 60 + ns * 12
    sounds = []
    for i in range(ns):
        rec = data[diroff + 60 + i * 12:]
        start, _audio_end, checksum = struct.unpack(">3I", rec[:12])
        nxt = data[diroff + 60 + (i + 1) * 12:diroff + 60 + (i + 1) * 12 + 4]
        end = struct.unpack(">I", nxt)[0] if i + 1 < ns else len(data) - base
        sounds.append({"index": i, "start": base + start, "end": base + end,
                       "checksum": checksum})
    return sounds


def parse_grain(data):
    """Split a granular bed into its SNR offset and pattern table."""
    hs = struct.unpack(">I", data[:4])[0]
    return {"snr_off": hs, "pattern": data[0x24:hs]}


def ram_end(data, snr_off, fallback):
    """Payload end from the RAM header extension, else the fallback bound."""
    import struct
    if snr_off + 20 <= len(data):
        size = struct.unpack(">I", data[snr_off + 8:snr_off + 12])[0] - 12
        end = snr_off + 20 + size
        if 0 < size and end <= len(data):
            return end
    return fallback


def decode_ea(info, payloads, ffmpeg, ffprobe, out_ogg, exact=True):
    """Decode the first candidate whose duration matches its header."""
    errors = []
    for head_size, payload in payloads:
        if not payload:
            continue
        riff = riff_xma2(payload, info["channels"], info["rate"], info["samples"])
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-y", "-v", "error",
             "-i", "pipe:0", "-map", "0:a", "-c:a", "libvorbis", "-q:a", "5",
             str(out_ogg)],
            input=riff, capture_output=True)
        if proc.returncode != 0:
            errors.append(f"head {head_size}: {proc.stderr.decode(errors='replace')[:120]}")
            continue
        if ffprobe is not None:
            try:
                if exact:
                    info["output_s"] = verify_duration(ffprobe, out_ogg, info)
                else:
                    info["output_s"] = verify_audible(ffprobe, out_ogg)
            except RuntimeError as error:
                errors.append(f"head {head_size}: {error}")
                continue
        else:
            info["output_s"] = info["samples"] / info["rate"]
        info["duration_s"] = info["samples"] / info["rate"]
        if "output_s" not in info:
            info["output_s"] = info["duration_s"]
        return info
    shown = errors if len(errors) <= 4 else errors[:3] + ["..."] + errors[-1:]
    raise RuntimeError("; ".join(shown) or "no payload candidates")


def verify_audible(ffprobe, path, minimum_s=0.1):
    """Loop segments only promise non-trivial audio, not a full length."""
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
    if actual < minimum_s:
        raise RuntimeError(f"output too short: {actual:.3f}s")
    return actual


def convert_sfx(game_root, out_dir, work, report, log, ffmpeg, workers=None):
    """Convert ABK banks, ambience beds, post beds and wheel stock."""
    from .audio_ffmpeg import probe_for
    game_root, out_dir, work = Path(game_root), Path(out_dir), Path(work)
    out_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    workers = workers or min(8, max(1, (os.cpu_count() or 2) // 2))
    ffprobe = probe_for(ffmpeg)
    manifest = {"version": 1, "banks": {}, "ambience": {}, "post": {}, "wheels": [],
                "pools": {}, "grains": {}}

    def track_entry(group, target, info, duration_s):
        return {"file": f"{group}/{target.name}", "samples": info["samples"],
                "rate": info["rate"], "channels": info["channels"],
                "loop": bool(info["loop"]), "duration_s": round(duration_s, 3)}

    def stereo_job(info, payload, target, exact=True):
        riff = riff_xma2(payload, info["channels"], info["rate"], info["samples"])
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-y", "-v", "error",
             "-i", "pipe:0", "-map", "0:a", "-c:a", "libvorbis", "-q:a", "5",
             str(target)],
            input=riff, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg decode failed: {proc.stderr.decode(errors='replace')[:200]}")
        if ffprobe is None:
            return info["samples"] / info["rate"]
        if exact:
            return verify_duration(ffprobe, target, info)
        return verify_audible(ffprobe, target)

    def layered_job(info, layers, target, scratch, exact=True):
        decode_layered(layers, info["channels"], info["rate"], info["samples"],
                       ffmpeg, target, scratch)
        if ffprobe is None:
            return info["samples"] / info["rate"]
        if exact:
            return verify_duration(ffprobe, target, info)
        return verify_audible(ffprobe, target)

    # ABK banks: every indexed RAM sound becomes banks/<stem>/<n>.ogg.
    abk_arch = BigArchive(game_root / "data/audio/audiofiles.big")
    bank_jobs = []
    for e in sorted(abk_arch.entries, key=lambda e: e.path):
        if not e.path.endswith(".abk"):
            continue
        data, sounds = parse_abk_data(abk_arch.read(e), e.path)
        bank_jobs.append((Path(e.path).stem, data, sounds))
    report(f"Converting {len(bank_jobs)} sound banks")

    def bank_job(args):
        stem, data, sounds = args
        bank_dir = out_dir / "banks" / stem
        bank_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for n, sound in enumerate(sounds):
            target = bank_dir / f"{n:03d}.ogg"
            try:
                if sound["streamed"]:
                    raise RuntimeError("streamed sound needs its .ast file")
                info = parse_snr(data[sound["snr_off"]:sound["snr_off"] + 8])
                end = ram_end(data, sound["snr_off"], sound["end"])
                region = data[sound["snr_off"]:end]
                want_layers = (info["channels"] + 1) // 2 if info["channels"] > 2 else 0
                if want_layers:
                    errors = []
                    duration = None
                    for skip in (8, 12, 20):
                        parts = deblock_eaxma_layers(region[skip:])
                        if len(parts) != want_layers:
                            continue
                        try:
                            duration = layered_job(
                                info, parts, target, work / "layers" / stem / f"{n:03d}",
                                exact=not info["loop"])
                        except (ValueError, RuntimeError) as error:
                            errors.append(str(error)[:100])
                            continue
                        break
                    if duration is None:
                        raise RuntimeError("; ".join(errors) or "no layered payload")
                else:
                    bounds = [end]
                    if sound["end"] != end:
                        bounds.append(sound["end"])
                    cands = []
                    seen = set()
                    # Trailing bank metadata (event names) can pollute the
                    # payload tail; trimmed variants let the duration check
                    # find the true end. Untrimmed candidates come first so
                    # clean sounds pay no extra decode.
                    trims = (0, 128, 512, 2048)
                    for bound in bounds:
                        for label, payload in list(ram_candidates(data, sound["snr_off"], bound)):
                            for trim in trims:
                                candidate = payload[:len(payload) - trim] if trim else payload
                                key = (label, len(candidate))
                                if key in seen or not candidate:
                                    continue
                                seen.add(key)
                                cands.append((f"{label}-{trim}" if trim else label, candidate))
                        for skip in (8, 12, 20):
                            payload = deblock_eaxma(data[sound["snr_off"] + skip:bound])
                            key = ("deblocked", hash(payload))
                            if not payload or key in seen:
                                continue
                            seen.add(key)
                            cands.append((f"deblocked+{skip}", payload))
                    decode_ea(info, cands, ffmpeg, ffprobe, target,
                              exact=not info["loop"])
                    duration = info["output_s"]
            except (ValueError, RuntimeError) as error:
                target.unlink(missing_ok=True)
                log.write(f"{stem} sound {n}: skipped ({error})\n")
                continue
            files.append({"file": f"banks/{stem}/{target.name}", "index": n,
                          "samples": info["samples"], "rate": info["rate"],
                          "channels": info["channels"], "loop": bool(info["loop"]),
                          "duration_s": round(duration, 3)})
        return (stem, files)

    # Ambience/post beds: .sns data paired with .snr descriptors by stem.
    # postresident.big is itself an EB BIG holding per-bed descriptors.
    resident = work / "postresident.big"
    post_arch = BigArchive(game_root / "data/audio/post.big")
    resident.write_bytes(post_arch.read(next(
        e for e in post_arch.entries if e.path.endswith(".big"))))
    res_arch = BigArchive(resident)
    res_heads = {Path(e.path).stem: res_arch.read(e) for e in res_arch.entries}
    amb_arch = BigArchive(game_root / "data/audio/ambienceresident.big")
    for e in amb_arch.entries:
        res_heads.setdefault(("amb", Path(e.path).stem), amb_arch.read(e))
    pair_jobs = []
    for big_name, group in (("ambience.big", "ambience"), ("post.big", "post")):
        arch = BigArchive(game_root / "data/audio" / big_name)
        for e in arch.entries:
            if not e.path.endswith(".sns"):
                continue
            stem = Path(e.path).stem
            key = ("amb", stem) if group == "ambience" else stem
            snr = res_heads.get(key) if group == "ambience" else res_heads.get(stem)
            pair_jobs.append((group, stem, arch.read(e), snr))

    def pair_job(args):
        group, stem, sns, snr = args
        target = out_dir / group / f"{stem}.ogg"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if snr is None:
                raise ValueError("no descriptor for this bed")
            info = parse_snr(snr)
            if info["channels"] > 2:
                duration = layered_job(info, deblock_eaxma_layers(sns), target, work / "layers" / stem)
            else:
                duration = stereo_job(info, deblock_eaxma(sns), target, exact=not info["loop"])
        except (ValueError, RuntimeError) as error:
            target.unlink(missing_ok=True)
            log.write(f"{group}/{stem}: skipped ({error})\n")
            return (group, stem, None)
        return (group, stem, track_entry(group, target, info, duration))

    # Wheels: standalone RAM stock.
    wheel_jobs = []
    wheels = BigArchive(game_root / "data/audio/wheels.big")
    for e in wheels.entries:
        if e.path.endswith(".snr"):
            wheel_jobs.append((Path(e.path).stem, wheels.read(e)))

    def wheel_job(args):
        stem, data = args
        target = out_dir / "wheels" / f"{stem}.ogg"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            info = parse_snr(data[:8])
            decode_ea(info, ram_candidates(data, 0, len(data)), ffmpeg, ffprobe, target)
        except (ValueError, RuntimeError) as error:
            target.unlink(missing_ok=True)
            log.write(f"wheels/{stem}: skipped ({error})\n")
            return (stem, None)
        return (stem, track_entry("wheels", target, info, info["output_s"]))

    def splc_job(args):
        stem, data = args
        pool_dir = out_dir / "pools" / stem
        pool_dir.mkdir(parents=True, exist_ok=True)
        try:
            sounds = parse_splc(data)
        except (ValueError, struct.error) as error:
            log.write(f"pools/{stem}: skipped ({error})\n")
            return (stem, [])
        files = []
        for sound in sounds:
            target = pool_dir / f"{sound['index']:04d}.ogg"
            try:
                blob = data[sound["start"]:sound["end"]]
                info = parse_snr(blob[:8])
                if info["version"] != 0 or info["codec"] != 3:
                    raise ValueError(f"unsupported stream: {info}")
                payload = deblock_eaxma(blob[8:])
                if not payload:
                    raise ValueError("empty payload after deblocking")
                duration = stereo_job(info, payload, target, exact=not info["loop"])
            except (ValueError, RuntimeError) as error:
                target.unlink(missing_ok=True)
                log.write(f"pools/{stem} sound {sound['index']}: skipped ({error})\n")
                continue
            files.append({"file": f"pools/{stem}/{target.name}", "index": sound["index"],
                          "samples": info["samples"], "rate": info["rate"],
                          "channels": info["channels"], "loop": bool(info["loop"]),
                          "duration_s": round(duration, 3)})
        return (stem, files)

    def grain_job(args):
        stem, data = args
        target = out_dir / "grains" / f"{stem}.ogg"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            grain = parse_grain(data)
            info = parse_snr(data[grain["snr_off"]:grain["snr_off"] + 8])
            if info["version"] != 0 or info["codec"] != 3:
                raise ValueError(f"unsupported stream: {info}")
            payload = deblock_eaxma(data[grain["snr_off"] + 8:])
            if not payload:
                raise ValueError("empty payload after deblocking")
            duration = stereo_job(info, payload, target, exact=not info["loop"])
        except (ValueError, RuntimeError) as error:
            target.unlink(missing_ok=True)
            log.write(f"grains/{stem}: skipped ({error})\n")
            return (stem, None)
        return (stem, track_entry("grains", target, info, duration))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for stem, files in pool.map(bank_job, bank_jobs):
            if files:
                manifest["banks"][stem] = files
        for group, stem, item in pool.map(pair_job, pair_jobs):
            if item:
                manifest[group][stem] = item
        for stem, item in pool.map(wheel_job, wheel_jobs):
            if item:
                manifest["wheels"].append(item)
        splc_jobs = []
        for e in abk_arch.entries:
            if e.path.endswith(".bnk"):
                splc_jobs.append((Path(e.path).stem, abk_arch.read(e)))
        report(f"Converting {len(splc_jobs)} random pools")
        for stem, files in pool.map(splc_job, splc_jobs):
            if files:
                manifest["pools"][stem] = files
        grain_arch = BigArchive(game_root / "data/audio/grains.big")
        grain_jobs = [(Path(e.path).stem, grain_arch.read(e)) for e in grain_arch.entries]
        for stem, item in pool.map(grain_job, grain_jobs):
            if item:
                manifest["grains"][stem] = item
        # SPLC random pools (collisions, menu, foley): indexed blobs.
        splc_arch = BigArchive(game_root / "data/audio/audiofiles.big")
        pool_jobs = []
        for e in splc_arch.entries:
            if e.path.endswith(".bnk"):
                pool_jobs.append((Path(e.path).stem, splc_arch.read(e)))
        for stem, files in pool.map(splc_job, pool_jobs):
            if files:
                manifest["pools"][stem] = files
        # Granular beds: single SNR each with a pitch pattern table.
        grain_arch = BigArchive(game_root / "data/audio/grains.big")
        grain_jobs = [(Path(e.path).stem, grain_arch.read(e)) for e in grain_arch.entries]
        for stem, item in pool.map(grain_job, grain_jobs):
            if item:
                manifest["grains"][stem] = item
    report(f"sfx: banks={len(manifest['banks'])} pools={len(manifest['pools'])} "
           f"ambience={len(manifest['ambience'])} post={len(manifest['post'])} "
           f"wheels={len(manifest['wheels'])} grains={len(manifest['grains'])}")
    (out_dir / "sfx.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest

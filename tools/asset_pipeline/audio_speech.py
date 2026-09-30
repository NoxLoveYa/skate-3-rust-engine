"""Retail speech lines (HDR/STH/DAT, Xbox 360 XMA2) for the audio asset group.

Pairing rule (reverse-engineered; only format facts are used here):
each speech big holds `<line>.dat` audio plus nested `<name>hdr.big` and
`<name>sth.big` line tables. A `.hdr` entry gives the stream count and a
u16be table of offsets into its `.sth` entry; each 12-byte `.sth` record is
an SNS offset plus the 8-byte EA SNR header. Sound N spans to the next
record's offset (last: EOF). Codec 3 (EAXMA) decodes via the shared
RIFF-XMA2 path; anything else is skipped with a log line.
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

SPEECH_GROUPS = ("announcerspeech", "cameramanspeech", "livingworldspeech", "nis")
# maincast is EALayer3 (no FFmpeg path); speech triggers need the .evt
# pick-records, so lines convert without event wiring for now.
SPEECH_LANGUAGE = "english"


def nested(big, name, work):
    """Materialize a nested EB BIG below the work dir for BigArchive."""
    target = work / Path(name).name
    if not target.is_file():
        target.write_bytes(big.read(next(e for e in big.entries if e.path == name)))
    return BigArchive(target)


def iter_sounds(hdr, sth, dat):
    num_sounds = hdr[3]
    stride = 2 + (hdr[2] & 0x7F)
    recs = []
    for n in range(1, num_sounds + 1):
        sth_off = struct.unpack(">H", hdr[0x10 + stride * (n - 1):][:2])[0]
        sns = struct.unpack(">I", sth[sth_off:sth_off + 4])[0]
        info = parse_snr(sth[sth_off + 4:sth_off + 12])
        recs.append((sns, info))
    for i, (sns, info) in enumerate(recs):
        end = recs[i + 1][0] if i + 1 < len(recs) else len(dat)
        yield i + 1, info, dat[sns:end]


def convert_speech(game_root, out_dir, work, report, log, ffmpeg, workers=None,
                   language=SPEECH_LANGUAGE, groups=SPEECH_GROUPS):
    """Convert speech lines; returns the speech manifest dict."""
    from .audio_ffmpeg import probe_for
    game_root, out_dir, work = Path(game_root), Path(out_dir), Path(work)
    out_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    workers = workers or min(8, max(1, (os.cpu_count() or 2) // 2))
    ffprobe = probe_for(ffmpeg)
    manifest = {"version": 1, "language": language, "groups": {}}
    for group in groups:
        big_path = game_root / "data/audio" / language / f"{group}.big"
        if not big_path.is_file():
            log.write(f"speech/{group}: archive missing, skipped\n")
            continue
        if group == "nis":
            convert_nis(big_path, language, game_root, out_dir, work, report, log,
                        ffmpeg, ffprobe, workers, manifest)
            continue
        report(f"Converting speech: {group}")
        big = BigArchive(big_path)
        try:
            hdr_name = next(e.path for e in big.entries if e.path.endswith("hdr.big"))
            sth_name = next(e.path for e in big.entries if e.path.endswith("sth.big"))
        except StopIteration:
            log.write(f"speech/{group}: no hdr/sth tables, skipped\n")
            continue
        headers, tables = nested(big, hdr_name, work), nested(big, sth_name, work)
        jobs = []
        for entry in big.entries:
            if not entry.path.endswith(".dat"):
                continue
            stem = Path(entry.path).stem
            try:
                hdr = headers.read(next(e for e in headers.entries if e.path == stem + ".hdr"))
                sth = tables.read(next(e for e in tables.entries if e.path == stem + ".sth"))
            except StopIteration:
                log.write(f"speech/{group}/{stem}: no hdr/sth entry, skipped\n")
                continue
            jobs.append((stem, hdr, sth, big.read(entry)))
        group_dir = out_dir / group
        group_dir.mkdir(exist_ok=True)

        def job(args):
            stem, hdr, sth, dat = args
            files = []
            for idx, info, blob in iter_sounds(hdr, sth, dat):
                target = group_dir / f"{stem}.s{idx}.ogg"
                try:
                    if info["version"] != 0 or info["codec"] != 3 or info["channels"] not in (1, 2):
                        raise ValueError(f"unsupported speech stream: {info}")
                    payload = deblock_eaxma(blob)
                    if not payload:
                        raise ValueError("empty payload after deblocking")
                    riff = riff_xma2(payload, info["channels"], info["rate"], info["samples"])
                    proc = subprocess.run(
                        [str(ffmpeg), "-hide_banner", "-y", "-v", "error",
                         "-i", "pipe:0", "-map", "0:a", "-c:a", "libvorbis", "-q:a", "5",
                         str(target)],
                        input=riff, capture_output=True)
                    if proc.returncode != 0:
                        raise RuntimeError(f"FFmpeg decode failed: {proc.stderr.decode(errors='replace')[:200]}")
                    if ffprobe is not None:
                        verify_duration(ffprobe, target, info)
                except (ValueError, RuntimeError) as error:
                    target.unlink(missing_ok=True)
                    log.write(f"speech/{group}/{stem}.s{idx}: skipped ({error})\n")
                    continue
                files.append({"file": f"{group}/{stem}.s{idx}.ogg", "line": stem,
                              "stream": idx, "samples": info["samples"],
                              "rate": info["rate"], "channels": info["channels"],
                              "duration_s": round(info["samples"] / info["rate"], 3)})
            return files

        count = 0
        all_files = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for files in pool.map(job, jobs):
                count += len(files)
                all_files.extend(files)
        manifest["groups"][group] = {"count": count, "files": all_files}
        report(f"speech/{group}: {count} lines converted")
    (out_dir / "speech.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def convert_nis(big_path, language, game_root, out_dir, work, report, log, ffmpeg, ffprobe, workers, manifest):
    """NIS lines are plain SNR+SNS splits paired by stem with the nested resident big."""
    big = BigArchive(big_path)
    resident_name = next(e.path for e in big.entries if e.path.endswith(".big"))
    res = nested(big, resident_name, work)
    heads = {}
    for e in res.entries:
        heads.setdefault(Path(e.path).stem, []).append(res.read(e))
    group_dir = out_dir / "nis"
    group_dir.mkdir(exist_ok=True)
    jobs = []
    for e in big.entries:
        stem = Path(e.path).stem
        for n, snr in enumerate(heads.get(stem, [])):
            jobs.append((stem, n, snr, big.read(e)))

    def job(args):
        stem, n, snr, sns = args
        target = group_dir / f"{stem}.s{n + 1}.ogg"
        try:
            info = parse_snr(snr)
            if info["version"] != 0 or info["codec"] != 3:
                raise ValueError(f"unsupported stream: {info}")
            if info["channels"] > 2:
                decode_layered(deblock_eaxma_layers(sns), info["channels"], info["rate"],
                               info["samples"], ffmpeg, target, group_dir / "layers" / stem)
            else:
                payload = deblock_eaxma(sns)
                if not payload:
                    raise ValueError("empty payload after deblocking")
                riff = riff_xma2(payload, info["channels"], info["rate"], info["samples"])
                proc = subprocess.run(
                    [str(ffmpeg), "-hide_banner", "-y", "-v", "error",
                     "-i", "pipe:0", "-map", "0:a", "-c:a", "libvorbis", "-q:a", "5",
                     str(target)],
                    input=riff, capture_output=True)
                if proc.returncode != 0:
                    raise RuntimeError(f"FFmpeg decode failed: {proc.stderr.decode(errors='replace')[:200]}")
            if ffprobe is not None:
                verify_duration(ffprobe, target, info)
        except (ValueError, RuntimeError) as error:
            target.unlink(missing_ok=True)
            log.write(f"speech/nis/{stem}.s{n + 1}: skipped ({error})\n")
            return None
        return {"file": f"nis/{target.name}", "line": stem, "stream": n + 1,
                "samples": info["samples"], "rate": info["rate"],
                "channels": info["channels"],
                "duration_s": round(info["samples"] / info["rate"], 3)}

    files = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(job, jobs):
            if item:
                files.append(item)
    manifest["groups"]["nis"] = {"count": len(files), "files": files}
    report(f"speech/nis: {len(files)} lines converted")

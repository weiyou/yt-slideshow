#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

"""
Turn a yt-summarize markdown file into a low-bitrate H.264/AAC MP4.

Each timestamped paragraph is spoken with macOS `say`. The picture is the
YouTube storyboard tile nearest that original timestamp, held for the length
of the speech. The overview, when present, plays first on the opening tile.

Usage:
    uv run yt-slideshow.py <summary.md>
    uv run yt-slideshow.py <summary.md> --out ~/Movies/talk.mp4
    uv run yt-slideshow.py <summary.md> --voice Samantha

Deps are stdlib only. Requires macOS `say`, ffmpeg/ffprobe, and yt-dlp.

Exit codes:
    0  mp4 written
    1  ordinary failure
    2  nothing timestamped to speak
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_SLIDES = 2

CACHE_DIR = Path(
    os.environ.get("YT_SLIDESHOW_CACHE_DIR", "~/.cache/yt-slideshow")
).expanduser()

YTDLP_BIN = os.environ.get("YT_SUMMARIZE_YTDLP", "yt-dlp")
FFMPEG_BIN = os.environ.get("YT_SLIDESHOW_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.environ.get("YT_SLIDESHOW_FFPROBE", "ffprobe")
SAY_BIN = os.environ.get("YT_SLIDESHOW_SAY", "say")

YTDLP_BASE_ARGS = ["--ignore-config", "--no-warnings", "--no-update"]

_COOKIES_ENV = os.environ.get(
    "YT_SLIDESHOW_COOKIES", os.environ.get("YT_SUMMARIZE_COOKIES")
)
DEFAULT_COOKIES_PATH = Path("~/cookies.txt").expanduser()

# 640x360 is a 2x present of the 320x180 storyboard. The source has no
# finer detail, so a higher frame size only costs bits.
FRAME_W = 640
FRAME_H = 360
VIDEO_FPS = 5
VIDEO_BITRATE = "280k"
VIDEO_MAXRATE = "400k"
VIDEO_BUFSIZE = "600k"
AUDIO_BITRATE = "64k"

STAMP_RE = re.compile(
    r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]"
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
RULE_RE = re.compile(r"^-{3,}\s*$")
YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/watch\?v=|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)
FILENAME_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
SKIP_SECTIONS = {"description", "overview"}


@dataclass
class Slide:
    t: float
    text: str
    label: str


def resolve_cookies_path(explicit: str | None = None) -> Path | None:
    """--cookies, then YT_SLIDESHOW_COOKIES / YT_SUMMARIZE_COOKIES, then ~/cookies.txt."""
    if explicit is not None and str(explicit).strip():
        p = Path(str(explicit)).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"cookies file not found: {p}")
        return p
    if _COOKIES_ENV is not None:
        raw = _COOKIES_ENV.strip()
        if raw.lower() in ("", "0", "none", "off", "false", "no"):
            return None
        p = Path(raw).expanduser()
        if not p.is_file():
            print(
                f"COOKIES: {raw!r} from env not found; continuing without",
                file=sys.stderr,
            )
            return None
        return p
    return DEFAULT_COOKIES_PATH if DEFAULT_COOKIES_PATH.is_file() else None


def which(binary: str) -> str | None:
    return shutil.which(binary) or (binary if Path(binary).is_file() else None)


def stamp_seconds(match: re.Match) -> float:
    if match.group(3) is not None:
        return (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + int(match.group(3))
        )
    return int(match.group(1)) * 60 + int(match.group(2))


def speakable(text: str) -> str:
    """Plain text for `say`. Embedded [[ commands are disarmed."""
    text = HTML_COMMENT_RE.sub(" ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?m)^>\s?", "", text)
    text = text.replace("[[", "[ [")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def heading_parts(line: str) -> tuple[int, str, float | None] | None:
    m = HEADING_RE.match(line.strip())
    if not m:
        return None
    level = len(m.group(1))
    raw = m.group(2).strip()
    stamp = None
    sm = STAMP_RE.search(raw)
    if sm and sm.end() == len(raw.rstrip()):
        stamp = stamp_seconds(sm)
        raw = raw[: sm.start()].strip()
    return level, raw, stamp


def leading_stamp(line: str) -> tuple[float, str] | None:
    m = STAMP_RE.match(line.strip())
    if not m:
        return None
    return stamp_seconds(m), line.strip()[m.end() :].strip()


def section_body(lines: list[str], name: str) -> str:
    """Body under a heading whose title equals `name`, until the next heading or rule."""
    want = name.strip().lower()
    collecting = False
    buf: list[str] = []
    for line in lines:
        if RULE_RE.match(line.strip()):
            if collecting:
                break
            continue
        parts = heading_parts(line)
        if parts is not None:
            if collecting:
                break
            _level, title, _stamp = parts
            collecting = title.strip().lower() == want
            continue
        if collecting and line.strip():
            buf.append(line)
    return speakable(" ".join(buf))


def video_id_from(text: str, filename: str) -> str | None:
    m = YOUTUBE_RE.search(text)
    if m:
        return m.group(1)
    m = FILENAME_ID_RE.search(filename)
    return m.group(1) if m else None


def parse_slides(text: str) -> list[Slide]:
    """Overview first (opening tile), then one slide per timestamped paragraph.

    A heading timestamp that repeats on the paragraph is one slide. The heading
    title is spoken once, ahead of the first paragraph in that section.
    """
    text = HTML_COMMENT_RE.sub("", text)
    lines = text.splitlines()

    article = lines
    for i, line in enumerate(lines):
        if RULE_RE.match(line.strip()):
            article = lines[i + 1 :]
            break

    slides: list[Slide] = []
    overview = section_body(lines, "Overview")
    if overview:
        slides.append(Slide(0.0, overview, "overview"))

    heading: str | None = None
    heading_stamp: float | None = None
    heading_pending = False
    skip_body = False
    para_t: float | None = None
    para_lines: list[str] = []

    def flush_para() -> None:
        nonlocal para_t, para_lines, heading_pending
        body = speakable(" ".join(para_lines))
        t = para_t
        para_lines = []
        para_t = None
        if t is None:
            return
        if heading and heading_pending:
            title = speakable(heading)
            label = title[:48]
            if body:
                if title[-1:] not in ".?!":
                    title += "."
                spoken = f"{title} [[slnc 350]] {body}"
            else:
                spoken = title
            heading_pending = False
        else:
            spoken = body
            label = spoken[:48]
        spoken = spoken.strip()
        if len(spoken) < 2:
            return
        slides.append(Slide(t, spoken, label))

    def flush_bare_heading() -> None:
        nonlocal heading_pending
        if heading and heading_pending and heading_stamp is not None:
            spoken = speakable(heading)
            if len(spoken) >= 2:
                slides.append(Slide(heading_stamp, spoken, spoken[:48]))
        heading_pending = False

    for line in article:
        parts = heading_parts(line)
        if parts is not None:
            level, title, stamp = parts
            if level < 2:
                continue
            flush_para()
            flush_bare_heading()
            if title.strip().lower() in SKIP_SECTIONS:
                heading = None
                heading_stamp = None
                heading_pending = False
                skip_body = True
                continue
            skip_body = False
            heading = title
            heading_stamp = stamp
            heading_pending = True
            continue
        if skip_body:
            continue
        if not line.strip():
            flush_para()
            continue
        stamped = leading_stamp(line)
        if stamped is not None:
            flush_para()
            para_t, rest = stamped
            para_lines = [rest] if rest else []
            continue
        if para_t is None and heading_stamp is not None and heading_pending:
            para_t = heading_stamp
        if para_t is not None:
            para_lines.append(line)
    flush_para()
    flush_bare_heading()
    return slides


def require_bin(name: str, binary: str) -> str:
    found = which(binary)
    if not found:
        raise RuntimeError(f"{name} not found ({binary})")
    return found


def run(cmd: list[str], *, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-8:])
        raise RuntimeError(f"{what} failed ({proc.returncode})\n{tail}")
    return proc


def download_storyboard(video_id: str, cookies: Path | None, ytdlp: str) -> tuple[Path, dict]:
    dest = CACHE_DIR / video_id
    dest.mkdir(parents=True, exist_ok=True)
    mhtml = dest / "sb.mhtml"
    info_path = dest / "sb.info.json"
    if not (mhtml.is_file() and info_path.is_file()):
        cmd = [
            ytdlp,
            *YTDLP_BASE_ARGS,
            "-f",
            "sb0",
            "--write-info-json",
            "-o",
            str(dest / "sb.%(ext)s"),
            "--no-playlist",
        ]
        if cookies is not None:
            cmd.extend(["--cookies", str(cookies)])
        cmd.append(f"https://www.youtube.com/watch?v={video_id}")
        run(cmd, what="yt-dlp storyboard")
    if not mhtml.is_file() or not info_path.is_file():
        raise RuntimeError(f"storyboard download produced no sb0 for {video_id}")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    spec = next(
        (f for f in info.get("formats") or [] if f.get("format_id") == "sb0"),
        None,
    )
    if not spec:
        raise RuntimeError(f"no sb0 format in storyboard info for {video_id}")
    return mhtml, {
        "width": int(spec["width"]),
        "height": int(spec["height"]),
        "columns": int(spec["columns"]),
        "rows": int(spec["rows"]),
        "fps": float(spec["fps"]),
        "duration": float(info.get("duration") or 0),
        "title": info.get("title") or "",
    }


def jpeg_parts(blob: bytes) -> list[bytes]:
    parts = []
    i = 0
    while True:
        start = blob.find(b"\xff\xd8\xff", i)
        if start < 0:
            break
        end = blob.find(b"\xff\xd9", start + 3)
        if end < 0:
            break
        parts.append(blob[start : end + 2])
        i = end + 2
    return parts


def extract_tile(
    mhtml: Path,
    spec: dict,
    index: int,
    ffmpeg: str,
    cache: Path,
) -> Path:
    """Crop one storyboard tile. Cached. Drops wide black bars (portrait video)."""
    out = cache / f"tile-{index:04d}.jpg"
    if out.is_file() and out.stat().st_size > 0:
        return out
    sheets_dir = cache / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    cols = spec["columns"]
    rows = spec["rows"]
    per = cols * rows
    si, rem = divmod(index, per)
    r, c = divmod(rem, cols)
    w, h = spec["width"], spec["height"]
    sheet = sheets_dir / f"sheet-{si:03d}.jpg"
    if not sheet.is_file():
        parts = jpeg_parts(mhtml.read_bytes())
        if si >= len(parts):
            raise RuntimeError(
                f"storyboard tile {index} is past the downloaded sheets ({len(parts)})"
            )
        sheet.write_bytes(parts[si])
    run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(sheet),
            "-vf", f"crop={w}:{h}:{c * w}:{r * h}",
            str(out),
        ],
        what="crop storyboard tile",
    )
    return out


def tile_index(t: float, spec: dict) -> int:
    fps = spec["fps"]
    if fps <= 0:
        return 0
    n = max(1, int(spec["duration"] * fps)) if spec["duration"] else 10**9
    # A tile covers [i/fps, (i+1)/fps).
    return max(0, min(int(t * fps), n - 1))


def speech_file(text: str, dest: Path, say: str, voice: str | None) -> float:
    script = dest.with_suffix(".txt")
    script.write_text(text + "\n", encoding="utf-8")
    aiff = dest.with_suffix(".aiff")
    cmd = [say, "-o", str(aiff), "-f", str(script)]
    if voice:
        cmd[1:1] = ["-v", voice]
    run(cmd, what="say")
    proc = run(
        [
            require_bin("ffprobe", FFPROBE_BIN),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(aiff),
        ],
        what="ffprobe",
    )
    try:
        dur = float((proc.stdout or "").strip())
    except ValueError as e:
        raise RuntimeError(f"could not read speech duration for {aiff}") from e
    if dur <= 0.05:
        raise RuntimeError(f"say produced empty audio for: {text[:80]!r}")
    return dur


def encode_segment(
    image: Path,
    audio: Path,
    duration: float,
    dest: Path,
    ffmpeg: str,
) -> None:
    # Storyboard jpegs are full-range. Tag the file as limited-range so a
    # phone player does not treat it as yuvj420p.
    vf = (
        f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease:"
        f"in_range=pc:out_range=tv,"
        f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={VIDEO_FPS},format=yuv420p"
    )
    run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", str(VIDEO_FPS),
            "-t", f"{duration:.3f}",
            "-i", str(image),
            "-i", str(audio),
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "stillimage",
            "-pix_fmt", "yuv420p",
            "-color_range", "tv",
            "-b:v", VIDEO_BITRATE,
            "-maxrate", VIDEO_MAXRATE,
            "-bufsize", VIDEO_BUFSIZE,
            "-c:a", "aac",
            "-b:a", AUDIO_BITRATE,
            "-ac", "1",
            str(dest),
        ],
        what="encode slide",
    )


def concat_segments(segments: list[Path], dest: Path, ffmpeg: str) -> None:
    listing = dest.with_suffix(".ffconcat")
    lines = ["ffconcat version 1.0"]
    for seg in segments:
        # concat demuxer wants a quoted path; escape quotes.
        path = str(seg.resolve()).replace("'", r"'\''")
        lines.append(f"file '{path}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    part = dest.with_suffix(".part.mp4")
    run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(listing),
            "-c", "copy",
            "-movflags", "+faststart",
            str(part),
        ],
        what="concat slides",
    )
    part.replace(dest)
    listing.unlink(missing_ok=True)


def render(md_path: Path, out_path: Path, voice: str | None, cookies: Path | None) -> None:
    text = md_path.read_text(encoding="utf-8")
    video_id = video_id_from(text, md_path.name)
    if not video_id:
        raise RuntimeError(
            f"no YouTube id in {md_path} (expected a watch?v= URL or [id] in the filename)"
        )
    slides = parse_slides(text)
    if not slides:
        print("NO_SLIDES: no timestamped paragraphs to speak", file=sys.stderr)
        raise SystemExit(EXIT_NO_SLIDES)

    say = require_bin("say", SAY_BIN)
    ffmpeg = require_bin("ffmpeg", FFMPEG_BIN)
    require_bin("ffprobe", FFPROBE_BIN)
    ytdlp = require_bin("yt-dlp", YTDLP_BIN)

    print(f"VIDEO_ID: {video_id}")
    print(f"SLIDES: {len(slides)}")
    mhtml, spec = download_storyboard(video_id, cookies, ytdlp)
    interval = (1.0 / spec["fps"]) if spec["fps"] else 0
    print(
        f"STORYBOARD: {spec['width']}x{spec['height']} "
        f"every {interval:.2f}s"
        + (f" duration {spec['duration']:.0f}s" if spec["duration"] else "")
    )
    if spec["title"]:
        print(f"TITLE: {spec['title']}")

    cache = CACHE_DIR / video_id
    out_path.parent.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="yt-slideshow-") as tmp:
        work = Path(tmp)
        for n, slide in enumerate(slides, start=1):
            idx = tile_index(slide.t, spec)
            print(
                f"SLIDE {n}/{len(slides)} t={slide.t:.1f}s tile={idx} {slide.label}",
                flush=True,
            )
            image = extract_tile(mhtml, spec, idx, ffmpeg, cache)
            audio = work / f"s{n:03d}.aiff"
            dur = speech_file(slide.text, audio, say, voice)
            seg = work / f"s{n:03d}.mp4"
            encode_segment(image, audio, dur, seg, ffmpeg)
            segments.append(seg)
            print(f"  spoke {dur:.1f}s")
        concat_segments(segments, out_path, ffmpeg)

    proc = run(
        [
            require_bin("ffprobe", FFPROBE_BIN),
            "-v", "error",
            "-show_entries", "format=duration,size",
            "-of", "json",
            str(out_path),
        ],
        what="ffprobe output",
    )
    meta = json.loads(proc.stdout or "{}").get("format") or {}
    print(f"MP4: {out_path.resolve()}")
    if meta.get("duration"):
        print(f"DURATION: {float(meta['duration']):.1f}")
    print(f"BYTES: {out_path.stat().st_size}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Speak a yt-summarize markdown file over its YouTube storyboard "
            "and write a low-bitrate H.264/AAC MP4."
        )
    )
    parser.add_argument("markdown", help="Path to a yt-summarize .md file")
    parser.add_argument(
        "--out",
        help="MP4 path (default: the markdown path with a .mp4 suffix)",
    )
    parser.add_argument(
        "--voice",
        default=os.environ.get("YT_SLIDESHOW_VOICE") or None,
        help="macOS say voice name (default: the system voice)",
    )
    parser.add_argument(
        "--cookies",
        help="Netscape cookies.txt for the storyboard fetch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the slides and exit without speech or download",
    )
    args = parser.parse_args()

    md_path = Path(args.markdown).expanduser()
    if not md_path.is_file():
        print(f"Error: not a file: {md_path}", file=sys.stderr)
        sys.exit(EXIT_FAIL)
    out_path = (
        Path(args.out).expanduser()
        if args.out
        else md_path.with_suffix(".mp4")
    )

    if args.dry_run:
        text = md_path.read_text(encoding="utf-8")
        vid = video_id_from(text, md_path.name)
        slides = parse_slides(text)
        print(f"VIDEO_ID: {vid or ''}")
        print(f"SLIDES: {len(slides)}")
        for n, slide in enumerate(slides, start=1):
            print(f"SLIDE {n} t={slide.t:.1f}s {slide.text[:120]}")
        if not slides:
            sys.exit(EXIT_NO_SLIDES)
        return

    try:
        cookies = resolve_cookies_path(args.cookies)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_FAIL)
    try:
        render(md_path, out_path, args.voice, cookies)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_FAIL)


if __name__ == "__main__":
    main()

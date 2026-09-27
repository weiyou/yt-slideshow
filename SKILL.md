---
name: yt-slideshow
description: >
  Turn a yt-summarize markdown file into a low-bitrate H.264/AAC MP4.
  macOS say reads each timestamped paragraph while the YouTube storyboard
  frame at that timestamp stays on screen. Use when the user wants the
  summary as a video to watch or send to their phone.
---

# yt-slideshow

One script call. Input is a yt-summarize `.md`. Output is an `.mp4` beside it:
640×360 H.264, AAC, small enough to open on a phone over the LAN.

The overview is spoken first, on the opening storyboard tile. Each paragraph
that starts with `[m:ss]` or `[h:mm:ss]` is one slide, on the tile nearest
that original timestamp. The description is not spoken.

## Run

The script is `yt-slideshow.py` in the same directory as this SKILL.md.
Resolve that directory to an absolute path (wherever this skill is installed).
Needs `uv` on PATH. `say`, `ffmpeg`, and `yt-dlp` must be on this Mac.

```bash
uv run <skill-dir>/yt-slideshow.py <path-to-summary.md>
```

- Pass the summary's path. Stay in the user's cwd. The mp4 is written next
  to the markdown (same name, `.mp4`).
- Bash tool parameter `timeout: 3700`. **Do not** prefix with `timeout` /
  `gtimeout` — macOS often lacks them; the tool param is enough.
- Defaults need no flags. Cookies: auto `~/cookies.txt`, same as yt-summarize.

Pass a flag only when the text after `</skill>` asks for it.

| Cue in the text after `</skill>` | Pass |
|----------------------------------|------|
| a macOS voice name | `--voice <name>` |
| a path for the video file | `--out <path>` |

## After the run

| Signal | Action |
|--------|--------|
| `MP4:` | One sentence naming that path and `DURATION`. **Stop.** |
| exit **2** / `NO_SLIDES:` | The file has no timestamped paragraphs. Say so. Stop. |
| exit **1** | Read the error. One retry. |

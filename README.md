# yt-slideshow

Speak a [yt-summarize](../yt-summarize) markdown file over the video's YouTube
storyboard and write a low-bitrate MP4.

```bash
uv run yt-slideshow.py path/to/summary.md
```

The mp4 is written next to the markdown. It is 640×360 H.264 and 64 kbps mono
AAC. Serve that folder with any static file server and open the file on a
phone.

`say` is the macOS speech synthesizer. Storyboard tiles (`sb0`, 320×180) are
cached under `~/.cache/yt-slideshow/<video-id>/`.

See [SKILL.md](SKILL.md) for how an agent should invoke it.

# Batch Video → GIF Converter

A small desktop GUI that turns a folder full of videos into high-quality animated GIFs in one batch. Point it at a folder (or hand-pick files), tweak a few settings, and convert everything at once. Existing GIFs in the input can be copied or moved straight through.

Conversion is done with **ffmpeg** under the hood, using a two-pass palette method for sharp colour and small file sizes.

## Requirements

- **Python 3.8+** — `tkinter` ships with the standard CPython installers, so no extra Python packages are needed.
- **ffmpeg** and **ffprobe** on your `PATH`:
  - **Windows:** download a build from https://www.gyan.dev/ffmpeg/builds/ and add its `bin/` folder to PATH
  - **macOS:** `brew install ffmpeg`
  - **Linux:** `sudo apt install ffmpeg`

If ffmpeg isn't found, the app still opens and tells you exactly what to install.

## Running it

```bash
python3 video2gif.py
```

## What it does

- **Batch conversion** — converts every selected video to a GIF in one run.
- **Many input formats** — mp4, mov, mkv, webm, avi, wmv, flv, m4v, mpg, ts, and more.
- **Only real videos** — files are verified with ffprobe, so non-videos (or audio-only files) are skipped automatically rather than producing broken GIFs.
- **Folder or files** — pick a whole folder, optionally including subfolders, or select individual files.
- **Pick-and-choose** — a checklist lets you tick exactly which files to convert; *Select all* / *Deselect all* / *Clear list* included.
- **Existing GIFs** — any `.gif` already in the input is passed through unchanged:
  - **Copy to output** (default) — leaves the original in place.
  - **Move (delete from input)** — copies to the output, then deletes the original (asks for confirmation first; never deletes a GIF that already lives in the output folder).
- **Safe output naming** — `clip.mov` and `clip.mp4` won't overwrite each other; names are auto-disambiguated.
- **Live feedback** — a *Total progress* bar for the batch plus a *Current file* bar for the active conversion, a timestamped log, a **Cancel** button, and an "open output folder" prompt when finished.

## GIF settings

| Setting | What it controls |
|---|---|
| **FPS** | Frames per second of the GIF. Lower = smaller file (e.g. 12–15 is plenty for most clips). |
| **Width px** | Output width in pixels; height scales automatically to keep the aspect ratio. `0` = keep original size. |
| **Max colors** | Palette size (2–256). Fewer colours = smaller file. |
| **Dither** | Colour-blending method: `sierra2_4a` (default, best general quality), `floyd_steinberg`, `bayer`, or `none`. |
| **Loop forever** | On = GIF loops endlessly; off = plays once. |
| **Hardware decoding** | Uses your GPU to decode the source video for a speed boost on large files. Falls back to CPU automatically if it fails. |

## Performance notes

- The two-pass method keeps memory usage flat, so long videos (well beyond a couple of minutes) convert smoothly instead of stalling.
- For long or high-resolution clips, the most effective ways to speed things up and shrink the output are **lowering FPS** and **reducing width** — both cut how much work ffmpeg has to do.
- Leave **Hardware decoding** on; only turn it off if you ever notice odd output on a specific file.

"""GTA 6 clip finder + Shorts maker.

Each run (hourly, GitHub Actions):
  1. For every active campaign streamer, list Twitch clips from the last 24h
     (clips viewers made = the moments chat went crazy), keep GTA 6 only.
  2. Score by attention speed = clip views per hour since it was made.
  3. Take the best clip not used before (respecting daily cap + spacing).
  4. Download Twitch's portrait (vertical) version, add captions + #ad label.
  5. Claude Haiku writes the title / hook; code adds tags + streamer credit.
  6. Publish as a GitHub Release (mp4 + copy-paste post text) for John's Grok.

Usage:
  python gta6_clipper.py --dry-run --count 3   # local test, writes to ./out
  python gta6_clipper.py --publish            # find, make, publish one Short
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yt_dlp

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = HERE / "state" / "posted.json"
UTC = dt.timezone.utc


def log(*a):
    print("[gta6]", *a, flush=True)


# ---------------------------------------------------------------- state
def load_state(repo=None):
    """With a repo: rebuild state from its gta6-* Releases (no commits, so the repo's
    website never redeploys). Without: local state file (dry runs / PC tests)."""
    if not repo:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {"next_number": 1, "posted": []}
    r = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/releases?per_page=100"],
                       capture_output=True, text=True, check=True)
    rels, dec, i, txt = [], json.JSONDecoder(), 0, r.stdout.strip()
    while i < len(txt):  # --paginate prints one JSON array per page, back to back
        page, i = dec.raw_decode(txt, i)
        rels += page
        while i < len(txt) and txt[i].isspace():
            i += 1
    posted, top = [], 0
    for rel in rels:
        m = re.fullmatch(r"gta6-(\d+)", rel.get("tag_name", ""))
        if not m:
            continue
        num = int(m.group(1)); top = max(top, num)
        meta = re.search(r"<!-- gta6 clip_id=(\S+) streamer=(\S+) -->", rel.get("body") or "")
        posted.append({"clip_id": meta.group(1) if meta else "", "number": num,
                       "date": rel["created_at"].replace("Z", "+00:00"),
                       "streamer": meta.group(2) if meta else ""})
    posted.sort(key=lambda p: p["date"])
    log(f"{len(posted)} earlier Shorts found on the Releases page")
    return {"next_number": top + 1, "posted": posted}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- find
def list_clips(streamer):
    url = f"https://www.twitch.tv/{streamer['twitch']}/clips?filter=last_day&range=24hr"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 15}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    return [e for e in (info.get("entries") or []) if e]


def clip_details(url):
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        return ydl.extract_info(url, download=False)


def find_candidates(state, limit_per_streamer=4):
    used = {p["clip_id"] for p in state["posted"]}
    now = time.time()
    out = []
    for s in CONFIG["streamers"]:
        if not s.get("active"):
            continue
        flat = []
        for attempt in range(2):  # Twitch sometimes returns an empty list once
            try:
                flat = list_clips(s)
            except Exception as e:  # one streamer failing must not stop the run
                log(f"list failed for {s['twitch']}: {e}")
            if flat:
                break
            time.sleep(3)
        log(f"{s['twitch']}: {len(flat)} clips in last 24h")
        flat = [e for e in flat if (e.get("view_count") or 0) >= CONFIG["min_twitch_views"]]
        flat.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
        checked = 0
        for e in flat:
            if checked >= limit_per_streamer:
                break
            url = e.get("url") or e.get("webpage_url")
            try:
                d = clip_details(url)
            except Exception as ex:
                log(f"details failed {url}: {ex}")
                continue
            checked += 1
            if d["id"] in used:
                continue
            if CONFIG["game_category"] not in (d.get("categories") or []):
                continue
            dur = d.get("duration") or 0
            if not (CONFIG["min_seconds"] <= dur <= CONFIG["max_seconds"] + 1):
                continue
            age_h = max((now - (d.get("timestamp") or now)) / 3600, 0.25)
            score = (d.get("view_count") or 0) / age_h
            out.append({"streamer": s, "info": d, "score": score, "age_h": age_h})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def quota_left(state):
    now = dt.datetime.now(UTC)
    today = [p for p in state["posted"] if p["date"][:10] == now.date().isoformat()]
    left = CONFIG["max_shorts_per_day"] - len(today)
    if state["posted"]:
        last = dt.datetime.fromisoformat(state["posted"][-1]["date"])
        if (now - last).total_seconds() < CONFIG["min_hours_between_shorts"] * 3600:
            return 0
    return max(0, min(left, CONFIG["max_shorts_per_run"]))


# ---------------------------------------------------------------- make
def download(info, workdir):
    fmt_ids = [f["format_id"] for f in info["formats"]]
    portrait = next((f for f in ("portrait-918", "portrait-720", "portrait-480") if f in fmt_ids), None)
    fmt = portrait or ("1080" if "1080" in fmt_ids else "best")
    path = workdir / "src.mp4"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "format": fmt,
                           "outtmpl": str(path), "overwrites": True}) as ydl:
        ydl.download([info["webpage_url"]])
    return path, bool(portrait)


def transcribe(src, workdir):
    from faster_whisper import WhisperModel
    model_path = os.environ.get("WHISPER_MODEL", "small.en")
    wav = workdir / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(wav)], check=True)
    import wave
    import numpy as np
    with wave.open(str(wav), "rb") as w:  # feed raw samples: avoids PyAV version issues
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    model = WhisperModel(model_path, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(audio, word_timestamps=True, vad_filter=True)
    words = []
    for seg in segs:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append((w.start, w.end, t))
    return words


def ass_time(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words, hook, disclosure, duration, path, font):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,2,2,60,60,470,1
Style: Hook,{font},78,&H0000F0FF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,2,8,60,60,120,1
Style: Ad,Arial,34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,0,0,3,2,0,7,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    lines.append(f"Dialogue: 2,{ass_time(0)},{ass_time(min(3.0, duration))},Ad,,0,0,0,,{ass_escape(disclosure)}\n")
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(2.8, duration))},Hook,,0,0,0,,{ass_escape(hook.upper())}\n")
    # captions: 3 words per card, uppercase, punctuation stripped (words empty = stream has its own captions)
    for i in range(0, len(words), 3):
        chunk = words[i:i + 3]
        start = chunk[0][0]
        end = words[i + 3][0] if i + 3 < len(words) else chunk[-1][1] + 0.3
        end = min(end, chunk[-1][1] + 0.6)
        clean = [re.sub(r"[^\w'?!-]", "", w[2]) for w in chunk]
        text = ass_escape(" ".join(t for t in clean if t).upper())
        if text:
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{text}\n")
    path.write_text("".join(lines), encoding="utf-8")


def render(src, is_portrait, ass_path, out_path, workdir):
    if is_portrait:
        vf = "scale=1080:1920:flags=lanczos,setsar=1"
    else:  # landscape fallback: blurred fill + centred gameplay
        vf = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=25:5[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    # fontsdir/ass paths are relative to workdir to dodge the Windows drive-colon filter bug
    vf += f",ass={ass_path.name}:fontsdir=."
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", src.name, "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", str(CONFIG["max_seconds"]),
           out_path.name]
    subprocess.run(cmd, check=True, cwd=workdir)


# ---------------------------------------------------------------- write post
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["title", "hook", "summary"],
    "additionalProperties": False,
}


def write_post(streamer, info, words):
    transcript = " ".join(w[2] for w in words)[:2500]
    fallback = {
        "title": f"{streamer['name']} in GTA 6: {info.get('title', '')}"[:70],
        "hook": "",
        "summary": info.get("title", ""),
    }
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("no ANTHROPIC_API_KEY - using fallback title")
        return fallback
    import anthropic
    prompt = f"""You write titles for a YouTube Shorts channel that posts funny / chaotic GTA 6 streamer moments.
Streamer: {streamer['name']}
Twitch clip title (written by a viewer who clipped it - the best clue to what the moment is): {info.get('title', '')}
What was said in the clip (auto transcript, may be messy): {transcript or '(no speech)'}

Return:
- title: max 60 characters. Start with the streamer's name and end with " | GTA 6" (write "GTA 6" only that once).
  Describe the funny/shocking thing that happens, built mainly on the viewer's clip title, specific and
  curiosity-driven, like "ohnePixel's Getaway Car Can Only Turn Right | GTA 6".
  No emojis, no clickbait lies, nothing that is not in the clip.
  The transcript is machine-made and often mishears words: never use a strange or made-up-looking word from it
  (place names, brand names, odd nouns). If the transcript is unclear, just rewrite the viewer's clip title
  into a punchier sentence. Plain everyday words only.
  Examples: viewer title "Is he about to do insurance fraud?" -> "KreekCraft Tries Insurance Fraud | GTA 6";
  viewer title "Throws her in the ocean" -> "AsianJeff Throws Her in the Ocean | GTA 6".
- hook: 2-5 words shown on screen for the first 3 seconds, e.g. "HE DID NOT PLAN THIS". No swear words
  (YouTube limits Shorts with profanity in the first seconds). Empty string if nothing fits.
- Title and summary must also contain no swear words.
- summary: one plain sentence describing the moment, for the description."""
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=CONFIG["haiku_model"],
            max_tokens=1024,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            log("Haiku refused - fallback title")
            return fallback
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
        title = data["title"].strip()
        if title.count("GTA 6") > 1 and title.endswith("| GTA 6"):
            title = title[: -len("| GTA 6")].rstrip(" |")
        if "GTA 6" not in title:
            title = f"{title} | GTA 6"
        data["title"] = title[:70]
        data["hook"] = data["hook"].strip()[:40]
        return data
    except Exception as e:
        log(f"Haiku call failed ({e}) - fallback title")
        return fallback


def post_text(streamer, info, post):
    tags = CONFIG["always_hashtags"] + streamer.get("hashtags", [])
    credit = f"@{streamer['youtube_handle'].lstrip('@')}" if streamer.get("youtube_handle") else streamer["name"]
    desc = "\n".join([
        f"{post['summary']}",
        "",
        f"Clip: {credit} streaming GTA 6 - {streamer['call_to_action']}",
        CONFIG["disclosure"],
        "",
        " ".join(tags),
    ])
    keywords = ["GTA 6", "GTA VI", f"{streamer['name']} GTA 6", streamer["name"], "GTA 6 funny moments",
                "GTA 6 clips", "GTA 6 streamer moments"]
    return post["title"], desc, keywords


# ---------------------------------------------------------------- publish
def gh_release(number, title, desc, keywords, streamer, info, mp4, repo):
    tag = f"gta6-{number:04d}"
    expires = (dt.datetime.now(UTC) + dt.timedelta(hours=CONFIG["release_expiry_hours"])).strftime("%Y-%m-%d %H:%M UTC")
    notes = f"""## Short #{number} - ready to post

**Post before:** {expires} (skip it after that)

### YouTube title
```
{title}
```

### YouTube description
```
{desc}
```

### Tags (YouTube "Tags" field)
```
{", ".join(keywords)}
```

### Campaign
{streamer['campaign']}
Source clip: {info['webpage_url']}

<!-- gta6 clip_id={info['id']} streamer={streamer['twitch']} -->
"""
    notes_file = mp4.parent / "notes.md"
    notes_file.write_text(notes, encoding="utf-8")
    subprocess.run(["gh", "release", "create", tag, str(mp4), "--repo", repo,
                    "--title", f"GTA6 #{number} - {title}", "--notes-file", str(notes_file)], check=True)
    return tag


def make_short(cand, workdir, number):
    streamer, info = cand["streamer"], cand["info"]
    workdir.mkdir(parents=True, exist_ok=True)
    font_src = os.environ.get("CAPTION_FONT_FILE")
    if font_src and Path(font_src).exists():
        shutil.copy(font_src, workdir / Path(font_src).name)
    font_name = os.environ.get("CAPTION_FONT_NAME", "Impact")
    src, is_portrait = download(info, workdir)
    words = transcribe(src, workdir)
    post = write_post(streamer, info, words)
    ass = workdir / "caps.ass"
    cap_words = [] if streamer.get("stream_has_own_captions") else words
    build_ass(cap_words, post["hook"], CONFIG["disclosure"], info.get("duration") or 30, ass, font_name)
    safe = re.sub(r"[^A-Za-z0-9]+", "-", post["title"]).strip("-")[:50]
    out = workdir / f"GTA6-{number:04d}-{safe}.mp4"
    render(src, is_portrait, ass, out, workdir)
    title, desc, keywords = post_text(streamer, info, post)
    return out, title, desc, keywords


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = ap.parse_args()

    state = load_state(args.repo if args.publish else None)
    n = args.count if args.dry_run else quota_left(state)
    if n <= 0:
        log("daily cap / spacing reached - nothing to do this run")
        return
    cands = find_candidates(state)
    log(f"{len(cands)} GTA 6 candidates")
    for c in cands[:8]:
        log(f"  {c['score']:8.1f} views/h  {c['info'].get('view_count'):>6} views  "
            f"{c['streamer']['name']}: {c['info'].get('title')!r}")
    made = 0
    for cand in cands:
        if made >= n:
            break
        number = state["next_number"]
        workdir = Path(args.out) / f"{number:04d}"
        try:
            mp4, title, desc, keywords = make_short(cand, workdir, number)
        except Exception as e:
            log(f"make failed for {cand['info'].get('webpage_url')}: {e}")
            continue
        if args.publish:
            gh_release(number, title, desc, keywords, cand["streamer"], cand["info"], mp4, args.repo)
        (workdir / "post.txt").write_text(f"TITLE:\n{title}\n\nDESCRIPTION:\n{desc}\n\nTAGS:\n{', '.join(keywords)}\n",
                                          encoding="utf-8")
        log(f"made #{number}: {title}  -> {mp4}")
        if not args.dry_run:
            state["posted"].append({"clip_id": cand["info"]["id"], "number": number,
                                    "date": dt.datetime.now(UTC).isoformat(), "title": title,
                                    "streamer": cand["streamer"]["twitch"]})
        state["next_number"] = number + 1
        made += 1
    if not args.dry_run and not args.publish:
        save_state(state)


if __name__ == "__main__":
    sys.exit(main())

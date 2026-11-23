"""GTA 6 clip finder + Shorts maker.

Each run (GitHub Actions, every 20 min; does nothing when the daily cap / spacing says wait):
  1. Pull the most-viewed English GTA 6 clips on ALL of Twitch from the last 24h (views = the crowd
     already proved it is a hype moment) plus clips of our campaign streamers (they pay, so boosted).
  2. Haiku judges the best few: 6 frames + timed transcript + loudness peaks -> hype score 1-10.
     Only min_hype+ is made. Talking, reviews, cutscenes, menus and context-only jokes are rejected.
  3. Cut to the action window, add hook + captions (+ #ad only for campaign clips), vertical 1080x1920.
  4. Publish as a GitHub Release (mp4 + copy-paste post text) for John's Grok.

Usage:
  python gta6_clipper.py --dry-run --count 3   # local test, writes to ./out
  python gta6_clipper.py --publish            # find, judge, make, publish one Short
  python gta6_clipper.py --check-quota        # prints go=true/false (cheap first workflow step)
"""
import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = HERE / "state" / "posted.json"
JUDGED_FILE = HERE / "state" / "judged.json"   # kept between cloud runs by actions/cache
BAD_KINDS = {"talking", "review_opinion", "cutscene", "menu_or_loading", "needs_context", "unsafe"}
UTC = dt.timezone.utc


def log(*a):
    print("[gta6]", *a, flush=True)


# ---------------------------------------------------------------- state
def load_state(repo=None):
    """With a repo: rebuild state from its gta6-* Releases (no commits needed).
    Without: local state file (dry runs / PC tests)."""
    if not repo:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {"next_number": CONFIG.get("number_floor", 0) + 1, "posted": []}
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
        num = int(m.group(1))
        top = max(top, num)
        meta = re.search(r"<!-- gta6 clip_id=(\S+) streamer=(\S+) -->", rel.get("body") or "")
        posted.append({"clip_id": meta.group(1) if meta else "", "number": num,
                       "date": rel["created_at"].replace("Z", "+00:00"),
                       "streamer": meta.group(2) if meta else ""})
    posted.sort(key=lambda p: p["date"])
    log(f"{len(posted)} earlier Shorts found on the Releases page")
    return {"next_number": max(top, CONFIG.get("number_floor", 0)) + 1, "posted": posted}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_judged():
    try:
        d = json.loads(JUDGED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    cutoff = time.time() - 48 * 3600
    return {k: v for k, v in d.items() if v.get("t", 0) > cutoff}


def save_judged(d):
    JUDGED_FILE.parent.mkdir(parents=True, exist_ok=True)
    JUDGED_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def quota_left(state):
    now = dt.datetime.now(UTC)
    today = [p for p in state["posted"] if p["date"][:10] == now.date().isoformat()]
    left = CONFIG["max_shorts_per_day"] - len(today)
    if state["posted"]:
        last = dt.datetime.fromisoformat(state["posted"][-1]["date"])
        if (now - last).total_seconds() < CONFIG["min_hours_between_shorts"] * 3600:
            return 0
    return max(0, min(left, CONFIG["max_shorts_per_run"]))


# ---------------------------------------------------------------- find
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's own public web client id, not a secret
CLIP_FIELDS = "slug title viewCount durationSeconds createdAt broadcaster{login displayName} game{name}"


def gql(query):
    req = urllib.request.Request(TWITCH_GQL, data=json.dumps({"query": query}).encode(),
                                 headers={"Client-ID": TWITCH_PUBLIC_CLIENT_ID, "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=25))
            if d.get("errors"):
                raise RuntimeError(d["errors"])
            return d.get("data") or {}
        except Exception as e:
            log(f"twitch query failed (try {attempt + 1}): {e}")
            time.sleep(3)
    return {}


def top_game_clips(period):
    q = ('query{ game(name:"%s"){ clips(first:100, criteria:{period:%s, sort:VIEWS_DESC, languages:[EN]})'
         '{ edges{ node{ %s } } } } }' % (CONFIG["game_category"], period, CLIP_FIELDS))
    g = gql(q).get("game") or {}
    return [e["node"] for e in (g.get("clips") or {}).get("edges", [])]


def streamer_clips(login):
    q = ('query{ user(login:"%s"){ clips(first:20, criteria:{period:LAST_DAY, sort:VIEWS_DESC})'
         '{ edges{ node{ %s } } } } }' % (login, CLIP_FIELDS))
    u = gql(q).get("user") or {}
    return [e["node"] for e in (u.get("clips") or {}).get("edges", [])]


def streamer_for(login, display):
    """Campaign streamer from config, or a plain credit entry for anyone else."""
    for st in CONFIG["streamers"]:
        if st["twitch"] == login:
            return st
    name = display or login
    return {"twitch": login, "name": name, "youtube_handle": "",
            "hashtags": ["#" + re.sub(r"[^A-Za-z0-9]", "", name)], "campaign": "",
            "call_to_action": f"Watch {name} live on Twitch: twitch.tv/{login}",
            "stream_has_own_captions": None}


def find_candidates(state):
    used = {p["clip_id"] for p in state["posted"]}
    now = dt.datetime.now(UTC)
    skip = set(CONFIG.get("skip_streamers", []))
    today = [p for p in state["posted"] if p["date"][:10] == now.date().isoformat()]
    blocked = {p["streamer"] for p in state["posted"]
               if (now - dt.datetime.fromisoformat(p["date"])).total_seconds()
               < CONFIG["same_streamer_gap_hours"] * 3600}
    for s in {p["streamer"] for p in today}:
        if sum(p["streamer"] == s for p in today) >= CONFIG["max_per_streamer_per_day"]:
            blocked.add(s)

    pool = {n["slug"]: n for n in top_game_clips("LAST_WEEK")}  # proven big moments
    pool.update({n["slug"]: n for n in top_game_clips("LAST_DAY")})  # fresh ones
    for st in CONFIG["streamers"]:
        if st.get("active"):
            for n in streamer_clips(st["twitch"]):
                pool.setdefault(n["slug"], n)
    log(f"{len(pool)} clips pulled from Twitch")

    out = []
    for n in pool.values():
        b = n.get("broadcaster") or {}
        login = b.get("login") or ""
        st = streamer_for(login, b.get("displayName"))
        is_campaign = bool(st.get("campaign")) and bool(st.get("active"))
        views = n.get("viewCount") or 0
        if (n.get("game") or {}).get("name") != CONFIG["game_category"]:
            continue
        if not login or n["slug"] in used or login in skip or login in blocked:
            continue
        if not (CONFIG["min_seconds"] <= (n.get("durationSeconds") or 0) <= 61):
            continue
        created = dt.datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00"))
        age_h = max((now - created).total_seconds() / 3600, 0.5)
        need = (CONFIG["campaign_min_views"] if is_campaign
                else CONFIG["min_clip_views"] if age_h <= 24 else CONFIG["min_older_clip_views"])
        if views < need:
            continue
        if age_h > CONFIG["clip_window_hours"]:
            continue
        score = views / (age_h ** 0.5) * (CONFIG["campaign_boost"] if is_campaign else 1)
        out.append({"streamer": st, "clip": n, "campaign": is_campaign, "score": score, "age_h": age_h,
                    "url": f"https://www.twitch.tv/{login}/clip/{n['slug']}"})
    out.sort(key=lambda c: c["score"], reverse=True)
    seen, short = {}, []  # variety: at most 2 clips per streamer in the shortlist
    for c in out:
        k = c["streamer"]["twitch"]
        if seen.get(k, 0) < 2:
            short.append(c)
            seen[k] = seen.get(k, 0) + 1
    return short


# ---------------------------------------------------------------- download + analyse
def clip_details(url):
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        return ydl.extract_info(url, download=False)


def download(info, workdir):
    import yt_dlp
    fmt_ids = [f["format_id"] for f in info["formats"]]
    portrait = next((f for f in ("portrait-918", "portrait-720", "portrait-480") if f in fmt_ids), None)
    fmt = portrait or ("1080" if "1080" in fmt_ids else "best")
    path = workdir / "src.mp4"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "format": fmt,
                           "outtmpl": str(path), "overwrites": True}) as ydl:
        ydl.download([info["webpage_url"]])
    return path, bool(portrait)


def transcribe(src, workdir):
    """Returns (words, loud_peaks)."""
    import wave
    import numpy as np
    from faster_whisper import WhisperModel
    wav = workdir / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(wav)], check=True)
    with wave.open(str(wav), "rb") as w:  # feed raw samples: avoids PyAV version issues
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    peaks = loud_peaks(audio)
    words = []
    if len(audio) < 16000:  # muted / silent clip
        return words, peaks
    try:
        model = WhisperModel(os.environ.get("WHISPER_MODEL", "small.en"), device="cpu", compute_type="int8")
        segs, _ = model.transcribe(audio, word_timestamps=True, vad_filter=True)
        for seg in segs:
            for w in seg.words or []:
                t = w.word.strip()
                if t:
                    words.append((w.start, w.end, t))
    except Exception as e:
        log(f"    transcript skipped: {e}")
    return words, peaks


def loud_peaks(audio, sr=16000, top=5):
    """Seconds where the clip is loudest (yelling, crashes) and dB above the clip's median."""
    import numpy as np
    hop = sr // 2
    n = len(audio) // hop
    if n == 0:
        return []
    rms = np.sqrt((audio[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-9)
    db = 20 * np.log10(rms)
    picks = []
    for i in np.argsort(db)[::-1]:
        if all(abs(int(i) - j) > 3 for j in picks):
            picks.append(int(i))
        if len(picks) >= top:
            break
    med = float(np.median(db))
    return [(round(i * 0.5, 1), round(float(db[i]) - med, 1)) for i in sorted(picks)]


def grab_frames(src, workdir, duration, count=6):
    frames = []
    for k in range(count):
        t = duration * (k + 0.5) / count
        f = workdir / f"f{k}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=-2:512", "-q:v", "5", str(f)], check=False)
        if f.exists():
            frames.append((round(t, 1), f))
    return frames


def timed_transcript(words):
    lines, cur, t0 = [], [], None
    for a, _, w in words:
        if t0 is None:
            t0 = a
        cur.append(w)
        if a - t0 >= 3:
            lines.append(f"[{t0:.1f}s] {' '.join(cur)}")
            cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:.1f}s] {' '.join(cur)}")
    return "\n".join(lines)[:3000]


# ---------------------------------------------------------------- judge + write post
SCHEMA = {
    "type": "object",
    "properties": {
        "hype": {"type": "integer"},
        "kind": {"type": "string", "enum": ["chaos", "fail", "funny_reaction", "scare", "stunt", "glitch",
                                            "wholesome", "talking", "review_opinion", "cutscene",
                                            "menu_or_loading", "needs_context", "unsafe", "other"]},
        "reason": {"type": "string"},
        "burned_captions": {"type": "boolean"},
        "start": {"type": "number"},
        "end": {"type": "number"},
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["hype", "kind", "reason", "burned_captions", "start", "end", "title", "hook", "summary"],
    "additionalProperties": False,
}


def judge_and_write(cand, duration, words, peaks, frames):
    """One Haiku call: how exciting is it (hype 1-10), where is the action, and the post text."""
    import anthropic
    clip, name = cand["clip"], cand["streamer"]["name"]
    prompt = f"""You pick clips for a YouTube Shorts channel of the most exciting, funny, crazy GTA 6 streamer moments.
Viewers scroll fast: a Short only works if something visibly happens and the payoff lands within seconds.

Clip by {name} on Twitch, {duration:.0f}s long, {clip.get('viewCount')} Twitch views in {cand['age_h']:.0f}h.
Viewer's clip title (best clue to the moment): {clip.get('title', '')!r}
Loudest moments (second, dB above the clip's normal level; yelling, crashes, explosions): {peaks}
Timed transcript (machine-made, mishears words):
{timed_transcript(words) or '(no speech)'}
The images are frames at these seconds: {[t for t, _ in frames]}.

Score "hype" 1-10 honestly and strictly. 8-10 = big crash, chaos, epic fail, scare, wild stunt, glitch, or a
huge funny reaction to something visible on screen; a stranger with no context would laugh or say "no way".
5-7 = mildly amusing. 1-4 = nothing much happens. Use the kinds talking / review_opinion / cutscene /
menu_or_loading / needs_context (score at most 4) when the clip is the streamer chatting, giving opinions or
ratings, watching a story cutscene, sitting in menus, or a joke that needs stream context.
Sexual humor, flirting, suggestive dancing, strip-club chaos and "gyatt" reactions are fine; that is part of
GTA and gets views, so score them like anything else on how funny/crazy the reaction is. Use "unsafe"
(score 1) ONLY when bare nipples or genitals are clearly and plainly visible in a frame, or for slurs or
real-world tragedy. Dark club scenes, underwear, bikinis, twerking and implied scenes are NOT unsafe. If you
are unsure, it is not unsafe.

Also return:
- burned_captions: true if the frames already show the streamer's own subtitle text on screen.
- start / end (seconds): the tightest window that keeps the setup and the payoff. Cut dead air and slow
  build-up before the action; keep 1-2 seconds after the payoff. At least 8 seconds. Use 0 and {duration:.0f}
  if the whole clip is needed.
- title: max 60 characters. Start with "{name}", end with " | GTA 6" ("GTA 6" written only once). Say what
  happens, specific and curiosity-driven, e.g. "KreekCraft Tries Insurance Fraud | GTA 6". No emojis, no
  swear words, nothing that is not in the clip. Never use odd words from the transcript; it mishears names.
- hook: 2-5 words on screen for the first 3 seconds that make people stay, e.g. "HE DID NOT PLAN THIS".
  No swear words.
- summary: one plain sentence describing the moment, no swear words."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(f.read_bytes()).decode()}}
               for _, f in frames]
    content.append({"type": "text", "text": prompt})
    resp = anthropic.Anthropic().messages.create(
        model=CONFIG["haiku_model"], max_tokens=1500,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        return None
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    title = data["title"].strip()
    if title.count("GTA 6") > 1 and title.endswith("| GTA 6"):
        title = title[: -len("| GTA 6")].rstrip(" |")
    if "GTA 6" not in title:
        title = f"{title} | GTA 6"
    data["title"] = title[:70]
    data["hook"] = data["hook"].strip()[:40]
    start = max(0.0, min(float(data["start"]), duration))
    end = min(float(data["end"]) or duration, duration, start + CONFIG["max_seconds"])
    if end - start < CONFIG["min_seconds"]:
        start, end = 0.0, min(duration, CONFIG["max_seconds"])
    data["start"], data["end"] = round(start, 2), round(end, 2)
    return data


# ---------------------------------------------------------------- render
def ass_time(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words, hook, disclosure, duration, path, font, offset=0.0):
    words = [(a - offset, b - offset, t) for a, b, t in words if a >= offset and a - offset < duration]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,2,2,60,60,470,1
Style: Hook,{font},84,&H0000F0FF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,8,3,8,60,60,120,1
Style: Ad,Arial,34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,0,0,3,2,0,7,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    if disclosure:
        lines.append(f"Dialogue: 2,{ass_time(0)},{ass_time(min(3.0, duration))},Ad,,0,0,0,,{ass_escape(disclosure)}\n")
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(2.8, duration))},Hook,,0,0,0,,"
                     f"{{\\fscx115\\fscy115\\t(0,250,\\fscx100\\fscy100)}}{ass_escape(hook.upper())}\n")
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


def render(src, is_portrait, ass_path, out_path, workdir, start, end):
    if is_portrait:
        vf = "scale=1080:1920:flags=lanczos,setsar=1"
    else:  # landscape fallback: blurred fill + gameplay as large as fits
        vf = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=25:5[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    vf += f",ass={ass_path.name}:fontsdir=."  # relative paths dodge the Windows drive-colon filter bug
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", src.name,
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path.name]
    subprocess.run(cmd, check=True, cwd=workdir)


# ---------------------------------------------------------------- post text + publish
def post_text(streamer, post, campaign):
    tags = CONFIG["always_hashtags"] + streamer.get("hashtags", [])
    credit = f"@{streamer['youtube_handle'].lstrip('@')}" if streamer.get("youtube_handle") else streamer["name"]
    desc = "\n".join([post["summary"], "", f"Clip: {credit} streaming GTA 6 - {streamer['call_to_action']}"]
                     + ([CONFIG["disclosure"]] if campaign else []) + ["", " ".join(tags)])
    keywords = ["GTA 6", "GTA VI", f"{streamer['name']} GTA 6", streamer["name"], "GTA 6 funny moments",
                "GTA 6 clips", "GTA 6 streamer moments"]
    return post["title"], desc, keywords


def gh_release(number, title, desc, keywords, cand, mp4, repo):
    streamer, j = cand["streamer"], cand["judge"]
    tag = f"gta6-{number:04d}"
    expires = (dt.datetime.now(UTC) + dt.timedelta(hours=CONFIG["release_expiry_hours"])).strftime("%Y-%m-%d %H:%M UTC")
    campaign = (f"{streamer['campaign']}\n**Submit this Short's link to that campaign within 30 minutes of posting.**"
                if cand["campaign"] else "None - just post it, nothing to submit.")
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
{campaign}

Source clip: {cand['url']}
Picked because: hype {j['hype']}/10 ({j['kind']}) - {j['reason']}

<!-- gta6 clip_id={cand['clip']['slug']} streamer={streamer['twitch']} -->
"""
    notes_file = mp4.parent / "notes.md"
    notes_file.write_text(notes, encoding="utf-8")
    subprocess.run(["gh", "release", "create", tag, str(mp4), "--repo", repo,
                    "--title", f"GTA6 #{number} - {title}", "--notes-file", str(notes_file)], check=True)
    return tag


# ---------------------------------------------------------------- pipeline
def evaluate(cand, workdir):
    """Download + transcribe + Haiku judge. Returns cand with judge data, or None."""
    workdir.mkdir(parents=True, exist_ok=True)
    info = clip_details(cand["url"])
    duration = float(info.get("duration") or cand["clip"].get("durationSeconds") or 30)
    src, is_portrait = download(info, workdir)
    words, peaks = transcribe(src, workdir)
    frames = grab_frames(src, workdir, duration)
    j = judge_and_write(cand, duration, words, peaks, frames)
    if not j:
        log("    judge returned nothing (refused)")
        return None
    log(f"    hype {j['hype']}/10 {j['kind']}  cut {j['start']}-{j['end']}s: {j['reason'][:140]}")
    cand.update(judge=j, src=src, is_portrait=is_portrait, words=words, duration=duration)
    return cand


def make_short(cand, workdir, number):
    streamer, j = cand["streamer"], cand["judge"]
    font_src = os.environ.get("CAPTION_FONT_FILE")
    if font_src and Path(font_src).exists():
        shutil.copy(font_src, workdir / Path(font_src).name)
    font_name = os.environ.get("CAPTION_FONT_NAME", "Impact")
    own_caps = streamer.get("stream_has_own_captions")
    if own_caps is None:
        own_caps = j["burned_captions"]
    ass = workdir / "caps.ass"
    build_ass([] if own_caps else cand["words"], j["hook"], CONFIG["disclosure"] if cand["campaign"] else "",
              j["end"] - j["start"], ass, font_name, offset=j["start"])
    safe = re.sub(r"[^A-Za-z0-9]+", "-", j["title"]).strip("-")[:50]
    out = workdir / f"GTA6-{number:04d}-{safe}.mp4"
    render(cand["src"], cand["is_portrait"], ass, out, workdir, j["start"], j["end"])
    title, desc, keywords = post_text(streamer, j, cand["campaign"])
    return out, title, desc, keywords


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--check-quota", action="store_true")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = ap.parse_args()

    state = load_state(args.repo if (args.publish or args.check_quota) else None)
    n = args.count if args.dry_run else quota_left(state)
    if args.check_quota:
        go = "true" if n > 0 else "false"
        print(f"go={go}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"go={go}\n")
        return
    if n <= 0:
        log("daily cap / spacing reached - nothing to do this run")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("ANTHROPIC_API_KEY missing - cannot judge clips")
        return 1
    cands = find_candidates(state)
    log(f"{len(cands)} GTA 6 candidates after filters")
    for c in cands[:12]:
        log(f"  {c['score']:8.0f} score {c['clip'].get('viewCount'):>6} views {c['age_h']:4.1f}h  "
            f"{'CAMPAIGN ' if c['campaign'] else ''}{c['streamer']['name']}: {c['clip'].get('title')!r}")
    # 1) judge up to max_judged_per_run fresh finalists (clips judged in the last 48h are reused, not re-paid)
    judged_db = load_judged()
    scored = []
    for cand in cands:
        if len(scored) >= CONFIG["max_judged_per_run"]:
            break
        slug = cand["clip"]["slug"]
        prev = judged_db.get(slug)
        if prev and (prev["hype"] < CONFIG["min_hype"] or prev["kind"] in BAD_KINDS):
            continue  # already rejected - skip without paying again
        workdir = Path(args.out) / slug[:30]
        log(f"judging {cand['url']}")
        try:
            got = evaluate(cand, workdir)
        except Exception as e:
            log(f"    judge failed: {e}")
            continue
        if not got:
            judged_db[slug] = {"hype": 0, "kind": "unsafe", "t": time.time()}
            continue
        j = got["judge"]
        judged_db[slug] = {"hype": j["hype"], "kind": j["kind"], "t": time.time()}
        got["workdir"] = workdir
        scored.append(got)
    save_judged(judged_db)

    # 2) make the best ones that clear the bar
    good = [c for c in scored if c["judge"]["hype"] >= CONFIG["min_hype"] and c["judge"]["kind"] not in BAD_KINDS]
    good.sort(key=lambda c: (c["judge"]["hype"], c["score"]), reverse=True)
    made = 0
    for cand in good[:n]:
        number = state["next_number"]
        workdir = cand["workdir"]
        try:
            mp4, title, desc, keywords = make_short(cand, workdir, number)
        except Exception as e:
            log(f"    make failed: {e}")
            continue
        if args.publish:
            gh_release(number, title, desc, keywords, cand, mp4, args.repo)
        j = cand["judge"]
        (workdir / "post.txt").write_text(
            f"TITLE:\n{title}\n\nDESCRIPTION:\n{desc}\n\nTAGS:\n{', '.join(keywords)}\n\n"
            f"HYPE: {j['hype']}/10 {j['kind']} - {j['reason']}\nSOURCE: {cand['url']}\n", encoding="utf-8")
        log(f"made #{number} (hype {j['hype']}): {title}  -> {mp4}")
        if not args.dry_run:
            state["posted"].append({"clip_id": cand["clip"]["slug"], "number": number,
                                    "date": dt.datetime.now(UTC).isoformat(), "streamer": cand["streamer"]["twitch"]})
        state["next_number"] = number + 1
        made += 1
    if made < n:
        log(f"only {made} clip(s) cleared hype {CONFIG['min_hype']}+ this run - quality first, nothing else published")
    if not args.dry_run and not args.publish:
        save_state(state)


if __name__ == "__main__":
    sys.exit(main())

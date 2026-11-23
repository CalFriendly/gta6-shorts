"""Copies every GTA6 Short on the Releases page into Desktop\\GTA6 Shorts (PC-side viewing only).
Removes local copies of releases that were deleted. Run by the Windows task "GTA6-Shorts-Sync".
Uses the public GitHub API without login (60 requests/hour is plenty)."""
import json
import re
import urllib.request
from pathlib import Path

REPO = "CalFriendly/gta6-shorts"
DEST = Path.home() / "Desktop" / "GTA6 Shorts"
LOG = Path(__file__).resolve().parent / "sync_to_desktop.log"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "gta6-shorts-sync", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=60)


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    rels = json.load(get(f"https://api.github.com/repos/{REPO}/releases?per_page=100"))
    wanted, got = set(), 0
    for rel in rels:
        if not re.fullmatch(r"gta6-\d+", rel.get("tag_name", "")):
            continue
        for a in rel.get("assets", []):
            if not a["name"].lower().endswith(".mp4"):
                continue
            wanted.add(a["name"])
            out = DEST / a["name"]
            if out.exists() and out.stat().st_size == a["size"]:
                continue
            tmp = out.with_suffix(".part")
            with get(a["browser_download_url"]) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            tmp.replace(out)
            got += 1
    removed = 0
    for f in DEST.glob("GTA6-*.mp4"):
        if f.name not in wanted:
            f.unlink()
            removed += 1
    LOG.write_text(f"ok: {len(wanted)} on releases, {got} downloaded, {removed} removed\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        LOG.write_text(f"error: {e}\n", encoding="utf-8")
        raise

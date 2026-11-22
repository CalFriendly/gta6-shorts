# GTA6 — campaign clip Shorts

Everything for the GTA 6 clip channel lives in this folder. This repo is only for GTA 6 clips and is
not connected to any other project.

## What it does (no PC needed)
Every hour, GitHub's cloud runs `gta6_clipper.py`:
1. Checks the Twitch clips of the campaign streamers in `config.json` (AsianJeff, KreekCraft, DiegoLoveless).
2. Keeps only Grand Theft Auto VI clips from the last 24 hours and picks the one gaining views fastest.
3. Makes a vertical 1080x1920 Short with a hook line, captions where needed and the #ad disclosure.
4. Publishes it as a **Release** named `GTA6 #N - <title>` with the MP4 attached.

There are at most 4 a day, at least 2 hours apart.

## Where the Shorts are
**https://github.com/CalFriendly/gta6-shorts/releases**

Only releases whose name starts with `GTA6 #` are clips. You don't need a GitHub login to download them.

## Rules for the poster (Grok)
Every 2 hours:
1. Open the releases page above.
2. For each `GTA6 #` release:
   - skip it if its "Post before" time has passed
   - skip it if a video with the same title is already on the channel
3. Download the `.mp4` under **Assets**.
4. Upload it to the GTA clips YouTube channel as a Short:
   - **Title**: paste exactly the text in the "YouTube title" box
   - **Description**: paste exactly the text in the "YouTube description" box
   - **Tags**: paste the "Tags" box into YouTube's Tags field
   - **Audience**: Not made for kids. Visibility: Public.
5. Within 30 minutes of it going live, submit the Short's link on Content Rewards to the campaign named
   under "Campaign" in the release.
6. Post them oldest first. Leave at least 1 hour between uploads.

## Changing things
- Streamers: edit `config.json` and set `"active": false` to pause one. Only clip streamers whose
  campaign you have joined.
- Daily amount: `max_shorts_per_day` in `config.json`.
- Stop everything: GitHub → Actions → "GTA6 Shorts" → ⋯ → Disable workflow.
- Run one now: Actions → "GTA6 Shorts" → Run workflow.

## Cost
- GitHub Actions: free (public repo).
- Claude Haiku titles: about $1–3 a month, using the `ANTHROPIC_API_KEY` repo secret.

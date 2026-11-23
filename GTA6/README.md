# GTA6 — campaign clip Shorts

Everything for the GTA 6 clip channel lives in this folder. This repo is only for GTA 6 clips and is
not connected to any other project.

## What it does (no PC needed)
Every 20 minutes, GitHub's cloud checks whether a Short is due. When one is, it runs `gta6_clipper.py`:
1. Pulls the most-viewed English GTA 6 clips on all of Twitch: the hottest from the last 24 hours, plus big
   launch-week moments with 15K+ views. Clips from our campaign streamers (`config.json`) get a boost
   because they pay.
2. Claude Haiku judges the top 5. It looks at frames, the transcript and the loudness peaks, and scores
   each one 1-10 for how crazy or funny it is.
   - It rejects talking, reviews, cutscenes, menus, jokes that need context, and visible nudity.
   - It keeps sexual humor.
3. Cuts the best one (6+) to the action. Adds a hook line, captions where needed, and an #ad label only
   for campaign clips.
4. Publishes it as a **Release** named `GTA6 #N - <title>` with the MP4 attached.

There are at most 4 a day, 2 hours apart, and at most 2 per streamer per day. If nothing scores high
enough, nothing is posted that run.

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
5. If the release's "Campaign" section names a campaign, submit the Short's link to it on Content Rewards
   within 30 minutes of it going live. If it says "None", there is nothing to submit.
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

# Write-up

## What changed vs. upstream DARK-PHOENIX

Deliberately minimal, targeted diff — the upstream architecture (Next.js/Vercel + Inngest +
Modal GPU + S3 + Prisma/Postgres + Gemini) is preserved unchanged.

1. **YouTube ingestion** (new): a URL input on the dashboard → `submitYouTubeUrl` server
   action validates the link, creates the same `UploadedFile` row shape as a file upload
   (plus new nullable `sourceUrl`/`youtubeVideoId` columns), and fires the existing Inngest
   event. The Inngest function gained one conditional step: rows with a `sourceUrl` are
   downloaded server-side by a new **CPU** Modal endpoint (`download_youtube`, yt-dlp) into
   the row's S3 key before the unchanged GPU processing step. The download runs inside
   Inngest via `step.fetch` because long downloads would exceed Vercel's serverless timeout.
   The old `ytdownload.py` prototype (hardcoded URL, `pytubefix`, not wired to anything) was
   left as-is rather than extended.
2. **LUNARTECH.AI watermark** (new): a `drawtext` filter chained into the *existing*
   subtitle burn-in ffmpeg pass in `create_subtitles_with_ffmpeg` — one render, no extra
   re-encode. Upper-right placement clear of the bottom-center captions, ~4% of frame
   width, white at 85% opacity on a translucent box, using the Anton font already baked
   into the image. Burned into the MP4 itself, verified by downloading clips from S3 and
   inspecting frames.
3. **Robustness fixes found by running the real workload** (details below).
4. **UI**: dark theme, sharp corners, ambient canvas background, restyled upload card.
   Cosmetic only — no upload/processing logic changed.

## What broke during deployment, and the fixes

Every one of these was diagnosed from logs/DB evidence on the live system:

1. **Vercel blocked deployment outright** — upstream pins Next.js 15.2.3/React 19.0.0,
   vulnerable to the Dec-2025 React2Shell RCE (CVE-2025-66478/CVE-2025-55182). Bumped to
   patched 15.5.16/19.2.6; `npm audit fix` for a critical `fast-xml-parser` advisory.
   `shadcn-dropzone`'s React-18 peer dependency conflicts with React 19; `.npmrc` with
   `legacy-peer-deps=true` fixes local and Vercel installs alike.
2. **Modal image build failures**: whisperx's old-style `setup.py` needs `pkg_resources`
   during build (gone from modern isolated build envs) and `av` (via faster-whisper) needs
   Cython to compile. Fixed by pre-installing `setuptools<81 wheel Cython` and installing
   requirements with `--no-build-isolation`. Heavy deps (`whisperx`/torch) are also imported
   lazily inside functions so `modal deploy` doesn't require multi-GB packages locally.
3. **Uploads stuck at "queued" forever**: the Inngest Vercel integration was installed
   before the Inngest organization existed, so *both* provisioned keys were stale. The
   signing key produced sync failures; the stale **event key** made `inngest.send()` drop
   every event silently (Inngest showed zero runs and zero `process-video-events` ever
   received). Fixed by copying the real keys from the Inngest dashboard into Vercel.
4. **Failures masked as success**: `step.fetch` resolves on any HTTP status, so a backend
   5xx marked rows "processed" with 0 clips. Now a non-2xx throws and the row is correctly
   marked "failed".
5. **Gemini free-tier limits**: 503 "high demand" (transient) and 429 — a full podcast's
   word-level transcript as a raw Python repr exceeds the 250K input-tokens/minute quota in
   a single request. The transcript is now compacted to `start-end word` lines (~4x
   smaller) and both error classes are retried with backoff.
6. **Active-speaker detection crashed** twice: unpinned `scenedetect` resolved to 0.6.6+
   which removed `scenedetect.video_manager` (pinned 0.6.3 in an image override layer,
   preserving the 40-minute cached build); and TalkNet's runtime `gdown --id` model
   downloads fail (flag removed in current gdown), so the TalkNet weights and S3FD face
   detector weights are now bundled into the image. The ASD subprocess also now surfaces
   its captured stderr instead of failing later with an opaque message.

## The assigned video and the YouTube datacenter-IP limitation

YouTube rejects downloads from cloud-provider IPs ("Sign in to confirm you're not a bot"),
which affects Modal exactly as the brief anticipates. Mitigations implemented: alternate
player clients, clear surfaced errors, and optional cookies-based authentication
(`YOUTUBE_COOKIES` secret) which makes the in-app YouTube flow work end-to-end.

For the assigned video (`YRvf00NooN8`), the **identical deployed pipeline** produced all
clips — transcription, moment selection, ASD, rendering, watermarking, and delivery all ran
on the deployed Modal backend. Only the initial fetch of the original bytes was performed
with yt-dlp from a residential IP and placed at the row's S3 key (the Inngest download step
skips when the object already exists, which also makes retries idempotent). The input was
not substituted, trimmed, or modified.

## What I'd improve with another week

- Move the reviewer-credit path to a proper admin/seed script and real Stripe webhooks
  end-to-end.
- Chunk very long transcripts and merge Gemini moment lists, removing the single-request
  token ceiling entirely.
- A queue-side progress UI (per-step status from Inngest) instead of a coarse status
  string, and automatic dashboard refresh.
- Replace the vendored TalkNet subprocess with an in-process call to drop the pickle
  file handoff and per-clip Python startup cost.
- Rotate every credential that touched local development and re-issue via a secrets
  manager before any real production use.

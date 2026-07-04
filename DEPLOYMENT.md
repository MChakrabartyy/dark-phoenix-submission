# Deployment

Live deployment of Dark Phoenix (AI podcast clipper), based on
[LUNARTECH-X/DARK-PHOENIX](https://github.com/LUNARTECH-X/DARK-PHOENIX).

## Live URLs

| Component | URL / identifier |
|---|---|
| Frontend (Vercel) | https://ai-podcast-clipper-frontend-gamma.vercel.app |
| GPU processing endpoint (Modal) | `https://mchakrabartyy--ai-podcast-clipper-aipodcastclipper-proce-1e0be5.modal.run` (bearer-token protected) |
| YouTube download endpoint (Modal, CPU) | `https://mchakrabartyy--ai-podcast-clipper-download-youtube.modal.run` (bearer-token protected) |
| Queue | Inngest Cloud (function `process-video`, event `process-video-events`) |
| Database | Supabase Postgres (via Supavisor **session pooler**, port 5432) |
| Storage | AWS S3, bucket `dark-phoenix-manisha-clips-090814040108-us-east-2-an` (us-east-2, private, CORS locked to the Vercel origin) |
| AI | Google Gemini API (`gemini-3-flash-preview`) |
| Payments | Stripe **test mode** only |

Reviewer login credentials are provided in the submission email, never in this repo.

## Architecture (unchanged from upstream, plus one new entry point)

```
Browser ── file upload (S3 presigned PUT) ──┐
Browser ── YouTube URL (new) ── server action creates DB row ──┤
                                                               ▼
                              Inngest Cloud (process-video-events)
                                   │  (YouTube rows only) step.fetch → Modal download_youtube → S3
                                   ▼
                        Modal L40S GPU: WhisperX transcription → Gemini moment
                        selection → TalkNet active-speaker detection → vertical
                        1080x1920 render → subtitle burn-in + LUNARTECH.AI
                        watermark (single ffmpeg pass) → clips uploaded to S3
                                   ▼
                        Clip rows created in Postgres; dashboard lists/plays
                        clips via presigned GET URLs
```

## Environment variables

Single source of truth: repo-root `.env` (gitignored) from `.env.example`. In production,
the same variables are set in Vercel project settings; the backend's subset lives in the
Modal secret `ai-podcast-clipper-secret` (note: frontend `PROCESS_VIDEO_ENDPOINT_AUTH` =
backend `AUTH_TOKEN`; the same bearer token also protects the download endpoint).
`INNGEST_EVENT_KEY` / `INNGEST_SIGNING_KEY` are provisioned by the Inngest Vercel
integration. Optional Modal secret key `YOUTUBE_COOKIES` (Netscape cookies.txt content)
enables YouTube downloads from datacenter IPs (see Limitations).

## Deploying from scratch

1. **Database**: create a Supabase project. Use the **session pooler** connection string
   (`aws-0-<region>.pooler.supabase.com:5432`, username `postgres.<ref>`) — the direct
   `db.<ref>.supabase.co` host is IPv6-only and unreachable from many networks, and the
   transaction pooler (6543) breaks Prisma prepared statements. Then:
   `cd ai-podcast-clipper-frontend && npm install && npm run db:push`.
2. **S3**: private bucket + an IAM user scoped to that bucket only (Put/Get/Delete/
   multipart on `bucket/*`, ListBucket on the bucket). CORS `AllowedOrigins` = the deployed
   frontend origin, methods PUT/GET/HEAD.
3. **Backend (Modal)**: `pip install modal && modal setup`, create the secret:
   `modal secret create ai-podcast-clipper-secret GEMINI_API_KEY=... AUTH_TOKEN=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=... S3_BUCKET_NAME=...`
   (the checked-in `setup_modal_secret.py` only *prints* the values — it does not persist
   the secret). Model weights (`asd/pretrain_TalkSet.model`, 63MB, and
   `asd/model/faceDetector/s3fd/sfd_face.pth`, 90MB) are not in git; download once with
   `gdown 1AbN9fCf9IexMxEKXLQY2KYBlb-IhSEea` / `gdown 1KafnHz7ccT-3IyddBsL5yi2xGtxAKypt`
   into those paths — they ship with the image via `add_local_dir`. Then
   `modal deploy main.py` (first image build ≈ 35-40 min). A Modal **payment method is
   required for GPU functions** even within free credit.
4. **Frontend (Vercel)**: import `ai-podcast-clipper-frontend` as the project root; set all
   env vars; deploy. Disable Deployment Protection (or configure a bypass) so Inngest can
   reach `/api/inngest`.
5. **Inngest**: install the Inngest integration from the Vercel Marketplace, then confirm
   the app appears under Apps in the Inngest dashboard. **If the Inngest organization was
   created after the integration**, the provisioned keys are stale: copy the real Signing
   Key and Event Key from the Inngest dashboard into Vercel (`vercel env add ... --force`)
   and redeploy — symptoms of stale keys are "Runs volume: 0" with rows stuck at "queued"
   (event key) or 401/"signature verification failed" on sync (signing key).
6. **Reviewer access**: the seeded account (credentials in the submission email) has
   credits granted directly in the database; Stripe stays in test mode and checkout is not
   required for review.

## Verification

- Wrong bearer token against either Modal endpoint → 401.
- `PUT /api/inngest` → `{"message":"Successfully registered"}`; Inngest dashboard shows the
  app synced with function `process-video`.
- End-to-end: upload a short MP4 → row goes queued → processing → processed and clips
  appear under My Clips.
- Watermark: download a produced MP4 from S3/My Clips and check the LUNARTECH.AI mark in
  the upper right — it is burned into the file by ffmpeg `drawtext`, not a web overlay.

## Known limitations

- **YouTube blocks downloads from datacenter IPs** ("Sign in to confirm you're not a
  bot"). The URL flow surfaces this as a clean failure (row marked Failed). Supplying a
  `YOUTUBE_COOKIES` key in the Modal secret (cookies.txt export of a logged-in YouTube
  session) makes datacenter downloads work. The assigned video was ingested with the exact
  same pipeline; only the fetch of the original bytes was performed from a residential IP
  (see WRITE_UP.md).
- Gemini free tier: 250K input tokens/min — the transcript is compacted before sending and
  429/503 responses are retried with backoff.
- `modal run main.py` (the `local_entrypoint`) is upstream dev scaffolding with a hardcoded
  test key; use the HTTP endpoints instead.

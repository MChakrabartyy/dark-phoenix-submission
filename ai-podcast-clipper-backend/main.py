import glob
import json
import pathlib
import pickle
import shutil
import subprocess
import time
import uuid
import boto3
import cv2
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import ffmpegcv
import modal
import numpy as np
from pydantic import BaseModel
import os
from google import genai

import pysubs2
from tqdm import tqdm
# whisperx is imported lazily inside AiPodcastClipper's methods (not here at module
# level) so that `modal deploy`/`modal run` can import this file locally without
# requiring torch/whisperx/pyannote-audio to be installed on the local machine.
# Those heavy, GPU-oriented packages only ever need to exist inside the remote
# Modal container image, which already installs them via requirements.txt.


class ProcessVideoRequest(BaseModel):
    s3_key: str


class DownloadYouTubeRequest(BaseModel):
    url: str
    s3_key: str


image = (modal.Image.from_registry(
    "nvidia/cuda:12.4.0-devel-ubuntu22.04", add_python="3.11")
    .apt_install(["ffmpeg", "libgl1-mesa-glx", "wget", "libcudnn8", "libcudnn8-dev", "pkg-config", "libavformat-dev", "libavcodec-dev", "libavdevice-dev", "libavutil-dev", "libswscale-dev", "libswresample-dev", "libavfilter-dev", "clang", "build-essential", "gcc", "git"])
    # whisperx's git checkout ships an old-style setup.py that imports pkg_resources
    # during its build step. pip's isolated build environment fetches a fresh
    # setuptools that no longer guarantees pkg_resources is importable, so we pin a
    # known-good setuptools/wheel pair here and disable build isolation so the
    # requirements install reuses this ambient setuptools instead. Disabling build
    # isolation means any transitive dependency that needs a source build (e.g. the
    # `av` package, pulled in by faster-whisper, has no prebuilt wheel here) no
    # longer gets its declared build-time requirements auto-installed, so Cython is
    # added here too.
    .pip_install("setuptools<81", "wheel", "Cython")
    .pip_install_from_requirements("requirements.txt", extra_options="--no-build-isolation")
    .run_commands([
        "mkdir -p /usr/share/fonts/truetype/custom",
        "wget -O /usr/share/fonts/truetype/custom/Anton-Regular.ttf https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
        "fc-cache -f -v",
    ])
    # Separate layer after the heavy requirements install so adding/upgrading
    # yt-dlp never invalidates the ~40-minute torch/whisperx build cache.
    .pip_install("yt-dlp")
    .add_local_dir("asd", "/asd", copy=True))

app = modal.App("ai-podcast-clipper", image=image)

volume = modal.Volume.from_name(
    "ai-podcast-clipper-model-cache", create_if_missing=True
)

mount_path = "/root/.cache/torch"

auth_scheme = HTTPBearer()


def create_vertical_video(tracks, scores, pyframes_path, pyavi_path, audio_path, output_path, framerate=25):
    target_width = 1080
    target_height = 1920

    flist = glob.glob(os.path.join(pyframes_path, "*.jpg"))
    flist.sort()

    faces = [[] for _ in range(len(flist))]

    for tidx, track in enumerate(tracks):
        score_array = scores[tidx]
        for fidx, frame in enumerate(track["track"]["frame"].tolist()):
            slice_start = max(fidx - 30, 0)
            slice_end = min(fidx + 30, len(score_array))
            score_slice = score_array[slice_start:slice_end]
            avg_score = float(np.mean(score_slice)
                              if len(score_slice) > 0 else 0)

            faces[frame].append(
                {'track': tidx, 'score': avg_score, 's': track['proc_track']["s"][fidx], 'x': track['proc_track']["x"][fidx], 'y': track['proc_track']["y"][fidx]})

    temp_video_path = os.path.join(pyavi_path, "video_only.mp4")

    vout = None
    for fidx, fname in tqdm(enumerate(flist), total=len(flist), desc="Creating vertical video"):
        img = cv2.imread(fname)
        if img is None:
            continue

        current_faces = faces[fidx]

        max_score_face = max(
            current_faces, key=lambda face: face['score']) if current_faces else None

        if max_score_face and max_score_face['score'] < 0:
            max_score_face = None

        if vout is None:
            vout = ffmpegcv.VideoWriterNV(
                file=temp_video_path,
                codec=None,
                fps=framerate,
                resize=(target_width, target_height)
            )

        if max_score_face:
            mode = "crop"
        else:
            mode = "resize"

        if mode == "resize":
            scale = target_width / img.shape[1]
            resized_height = int(img.shape[0] * scale)
            resized_image = cv2.resize(
                img, (target_width, resized_height), interpolation=cv2.INTER_AREA)

            scale_for_bg = max(
                target_width / img.shape[1], target_height / img.shape[0])
            bg_width = int(img.shape[1] * scale_for_bg)
            bg_heigth = int(img.shape[0] * scale_for_bg)

            blurred_background = cv2.resize(img, (bg_width, bg_heigth))
            blurred_background = cv2.GaussianBlur(
                blurred_background, (121, 121), 0)

            crop_x = (bg_width - target_width) // 2
            crop_y = (bg_heigth - target_height) // 2
            blurred_background = blurred_background[crop_y:crop_y +
                                                    target_height, crop_x:crop_x + target_width]

            center_y = (target_height - resized_height) // 2
            blurred_background[center_y:center_y +
                               resized_height, :] = resized_image

            vout.write(blurred_background)

        elif mode == "crop":
            scale = target_height / img.shape[0]
            resized_image = cv2.resize(
                img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            frame_width = resized_image.shape[1]

            center_x = int(
                max_score_face["x"] * scale if max_score_face else frame_width // 2)
            top_x = max(min(center_x - target_width // 2,
                        frame_width - target_width), 0)

            image_cropped = resized_image[0:target_height,
                                          top_x:top_x + target_width]

            vout.write(image_cropped)

    if vout:
        vout.release()

    ffmpeg_command = (f"ffmpeg -y -i {temp_video_path} -i {audio_path} "
                      f"-c:v h264 -preset fast -crf 23 -c:a aac -b:a 128k "
                      f"{output_path}")
    subprocess.run(ffmpeg_command, shell=True, check=True, text=True)


def create_subtitles_with_ffmpeg(transcript_segments: list, clip_start: float, clip_end: float, clip_video_path: str, output_path: str, max_words: int = 5):
    temp_dir = os.path.dirname(output_path)
    subtitle_path = os.path.join(temp_dir, "temp_subtitles.ass")

    clip_segments = [segment for segment in transcript_segments
                     if segment.get("start") is not None
                     and segment.get("end") is not None
                     and segment.get("end") > clip_start
                     and segment.get("start") < clip_end
                     ]

    subtitles = []
    current_words = []
    current_start = None
    current_end = None

    for segment in clip_segments:
        word = segment.get("word", "").strip()
        seg_start = segment.get("start")
        seg_end = segment.get("end")

        if not word or seg_start is None or seg_end is None:
            continue

        start_rel = max(0.0, seg_start - clip_start)
        end_rel = max(0.0, seg_end - clip_start)

        if end_rel <= 0:
            continue

        if not current_words:
            current_start = start_rel
            current_end = end_rel
            current_words = [word]
        elif len(current_words) >= max_words:
            subtitles.append(
                (current_start, current_end, ' '.join(current_words)))
            current_words = [word]
            current_start = start_rel
            current_end = end_rel
        else:
            current_words.append(word)
            current_end = end_rel

    if current_words:
        subtitles.append(
            (current_start, current_end, ' '.join(current_words)))

    subs = pysubs2.SSAFile()

    subs.info["WrapStyle"] = 0
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["PlayResX"] = 1080
    subs.info["PlayResY"] = 1920
    subs.info["ScriptType"] = "v4.00+"

    style_name = "Default"
    new_style = pysubs2.SSAStyle()
    new_style.fontname = "Anton"
    new_style.fontsize = 140
    new_style.primarycolor = pysubs2.Color(255, 255, 255)
    new_style.outline = 2.0
    new_style.shadow = 2.0
    new_style.shadowcolor = pysubs2.Color(0, 0, 0, 128)
    new_style.alignment = 2
    new_style.marginl = 50
    new_style.marginr = 50
    new_style.marginv = 50
    new_style.spacing = 0.0

    subs.styles[style_name] = new_style

    for i, (start, end, text) in enumerate(subtitles):
        start_time = pysubs2.make_time(s=start)
        end_time = pysubs2.make_time(s=end)
        line = pysubs2.SSAEvent(
            start=start_time, end=end_time, text=text, style=style_name)
        subs.events.append(line)

    subs.save(subtitle_path)

    # Small, discreet LUNARTECH.AI watermark burned into the exported file itself
    # (not a web-only overlay). Chained into the same ffmpeg pass as the subtitle
    # burn-in to avoid a second re-encode. Placed in the upper-right safe area so
    # it never overlaps the bottom-center captions (alignment=2, marginv=50 above)
    # or a speaker's face. fontsize=42 on a 1080px-wide frame is ~3.9% of width,
    # within the brief's recommended 3-5% range. Uses the Anton font already
    # downloaded into the image for subtitles, so no new font asset is needed.
    watermark_filter = (
        "drawtext=fontfile=/usr/share/fonts/truetype/custom/Anton-Regular.ttf:"
        "text='LUNARTECH.AI':fontcolor=white@0.85:fontsize=42:"
        "x=w-tw-24:y=24:box=1:boxcolor=black@0.35:boxborderw=10"
    )

    ffmpeg_cmd = (f"ffmpeg -y -i {clip_video_path} -vf \"ass={subtitle_path},{watermark_filter}\" "
                  f"-c:v h264 -preset fast -crf 23 {output_path}")

    subprocess.run(ffmpeg_cmd, shell=True, check=True)


def process_clip(base_dir: str, original_video_path: str, s3_key: str, start_time: float, end_time: float, clip_index: int, transcript_segments: list):
    clip_name = f"clip_{clip_index}"
    s3_key_dir = os.path.dirname(s3_key)
    output_s3_key = f"{s3_key_dir}/{clip_name}.mp4"
    print(f"Output S3 key: {output_s3_key}")

    clip_dir = base_dir / clip_name
    clip_dir.mkdir(parents=True, exist_ok=True)

    clip_segment_path = clip_dir / f"{clip_name}_segment.mp4"
    vertical_mp4_path = clip_dir / "pyavi" / "video_out_vertical.mp4"
    subtitle_output_path = clip_dir / "pyavi" / "video_with_subtitles.mp4"

    (clip_dir / "pywork").mkdir(exist_ok=True)
    pyframes_path = clip_dir / "pyframes"
    pyavi_path = clip_dir / "pyavi"
    audio_path = clip_dir / "pyavi" / "audio.wav"

    pyframes_path.mkdir(exist_ok=True)
    pyavi_path.mkdir(exist_ok=True)

    duration = end_time - start_time
    cut_command = (f"ffmpeg -i {original_video_path} -ss {start_time} -t {duration} "
                   f"{clip_segment_path}")
    subprocess.run(cut_command, shell=True, check=True,
                   capture_output=True, text=True)

    extract_cmd = f"ffmpeg -i {clip_segment_path} -vn -acodec pcm_s16le -ar 16000 -ac 1 {audio_path}"
    subprocess.run(extract_cmd, shell=True,
                   check=True, capture_output=True)

    shutil.copy(clip_segment_path, base_dir / f"{clip_name}.mp4")

    columbia_command = (f"python demoTalkNet.py --videoName {clip_name} "
                        f"--videoFolder {str(base_dir)} "
                        f"--pretrainModel pretrain_TalkSet.model")

    columbia_start_time = time.time()
    # check=True + captured output so an ASD failure surfaces its real error
    # instead of only manifesting later as "Tracks or scores not found".
    asd_result = subprocess.run(columbia_command, cwd="/asd", shell=True,
                                capture_output=True, text=True)
    if asd_result.returncode != 0:
        print(f"TalkNet ASD failed (exit {asd_result.returncode}):\n"
              f"stdout: {asd_result.stdout[-2000:]}\nstderr: {asd_result.stderr[-2000:]}")
        raise RuntimeError(f"Active-speaker detection failed for {clip_name}")
    columbia_end_time = time.time()
    print(
        f"Columbia script completed in {columbia_end_time - columbia_start_time:.2f} seconds")

    tracks_path = clip_dir / "pywork" / "tracks.pckl"
    scores_path = clip_dir / "pywork" / "scores.pckl"
    if not tracks_path.exists() or not scores_path.exists():
        raise FileNotFoundError("Tracks or scores not found for clip")

    with open(tracks_path, "rb") as f:
        tracks = pickle.load(f)

    with open(scores_path, "rb") as f:
        scores = pickle.load(f)

    cvv_start_time = time.time()
    create_vertical_video(
        tracks, scores, pyframes_path, pyavi_path, audio_path, vertical_mp4_path
    )
    cvv_end_time = time.time()
    print(
        f"Clip {clip_index} vertical video creation time: {cvv_end_time - cvv_start_time:.2f} seconds")

    create_subtitles_with_ffmpeg(transcript_segments, start_time,
                                 end_time, vertical_mp4_path, subtitle_output_path, max_words=5)

    s3_client = boto3.client("s3")
    s3_client.upload_file(
        subtitle_output_path, os.environ["S3_BUCKET_NAME"], output_s3_key)


@app.cls(gpu="L40S", timeout=3600, retries=0, scaledown_window=20, secrets=[modal.Secret.from_name("ai-podcast-clipper-secret")], volumes={mount_path: volume})
class AiPodcastClipper:
    @modal.enter()
    def load_model(self):
        import whisperx

        print("Loading models")

        self.whisperx_model = whisperx.load_model(
            "large-v2", device="cuda", compute_type="float16")

        self.alignment_model, self.metadata = whisperx.load_align_model(
            language_code="en",
            device="cuda"
        )

        print("Transcription models loaded...")

        print("Creating gemini client...")
        self.gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        print("Created gemini client...")

    def transcribe_video(self, base_dir: str, video_path: str) -> str:
        import whisperx

        audio_path = base_dir / "audio.wav"
        extract_cmd = f"ffmpeg -i {video_path} -vn -acodec pcm_s16le -ar 16000 -ac 1 {audio_path}"
        subprocess.run(extract_cmd, shell=True,
                       check=True, capture_output=True)

        print("Starting transcription with WhisperX...")
        start_time = time.time()

        audio = whisperx.load_audio(str(audio_path))
        result = self.whisperx_model.transcribe(audio, batch_size=16)

        result = whisperx.align(
            result["segments"],
            self.alignment_model,
            self.metadata,
            audio,
            device="cuda",
            return_char_alignments=False
        )

        duration = time.time() - start_time
        print("Transcription and alignment took " + str(duration) + " seconds")

        segments = []

        if "word_segments" in result:
            for word_segment in result["word_segments"]:
                # Skip words without timing data (common with punctuation or alignment failures)
                if "start" not in word_segment or "end" not in word_segment:
                    continue
                segments.append({
                    "start": word_segment["start"],
                    "end": word_segment["end"],
                    "word": word_segment.get("word", ""),
                })

        return json.dumps(segments)

    def identify_moments(self, transcript: list):
        from google.genai import errors as genai_errors

        # The raw list-of-dicts repr of a full podcast's word-level transcript
        # exceeds Gemini's free-tier 250K input-tokens/minute quota in a single
        # request, so compact each word to "start-end word" lines (~4x smaller)
        # before sending.
        compact_lines = []
        for seg in transcript:
            word = str(seg.get("word", "")).strip()
            if not word:
                continue
            start = seg.get("start")
            end = seg.get("end")
            if start is None or end is None:
                continue
            compact_lines.append(f"{start:.1f}-{end:.1f} {word}")
        compact_transcript = "\n".join(compact_lines)

        # Retry transient 503s ("high demand") and per-minute 429 quota windows.
        last_error = None
        for attempt in range(5):
            try:
                return self._identify_moments_once(compact_transcript)
            except genai_errors.ServerError as e:
                last_error = e
                wait = 2 ** attempt * 5
                print(f"Gemini unavailable (attempt {attempt + 1}/5), retrying in {wait}s: {e}")
                time.sleep(wait)
            except genai_errors.ClientError as e:
                if getattr(e, "code", None) != 429 and "429" not in str(e):
                    raise
                last_error = e
                print(f"Gemini per-minute quota hit (attempt {attempt + 1}/5), waiting 65s")
                time.sleep(65)
        raise last_error

    def _identify_moments_once(self, transcript: str):
        response = self.gemini_client.models.generate_content(model="gemini-3-flash-preview", contents="""
    This is a podcast video transcript. Each line has the format "START-END word", where START and END are that word's start and end times in seconds. I am looking to create clips between a minimum of 30 and maximum of 60 seconds long. The clip should never exceed 60 seconds.

    Your task is to find and extract stories, or question and their corresponding answers from the transcript.
    Each clip should begin with the question and conclude with the answer.
    It is acceptable for the clip to include a few additional sentences before a question if it aids in contextualizing the question.

    Please adhere to the following rules:
    - Ensure that clips do not overlap with one another.
    - Start and end timestamps of the clips should align perfectly with the sentence boundaries in the transcript.
    - Only use the start and end timestamps provided in the input. modifying timestamps is not allowed.
    - Format the output as a list of JSON objects, each representing a clip with 'start' and 'end' timestamps: [{"start": seconds, "end": seconds}, ...clip2, clip3]. The output should always be readable by the python json.loads function.
    - Aim to generate longer clips between 40-60 seconds, and ensure to include as much content from the context as viable.

    Avoid including:
    - Moments of greeting, thanking, or saying goodbye.
    - Non-question and answer interactions.

    If there are no valid clips to extract, the output should be an empty list [], in JSON format. Also readable by json.loads() in Python.

    The transcript is as follows:\n\n""" + str(transcript))
        print(f"Identified moments response: ${response.text}")
        return response.text

    @modal.fastapi_endpoint(method="POST")
    def process_video(self, request: ProcessVideoRequest, token: HTTPAuthorizationCredentials = Depends(auth_scheme)):
        s3_key = request.s3_key

        if token.credentials != os.environ["AUTH_TOKEN"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Incorrect bearer token", headers={"WWW-Authenticate": "Bearer"})

        run_id = str(uuid.uuid4())
        base_dir = pathlib.Path("/tmp") / run_id
        base_dir.mkdir(parents=True, exist_ok=True)

        # Download video file
        video_path = base_dir / "input.mp4"
        s3_client = boto3.client("s3")
        s3_client.download_file(os.environ["S3_BUCKET_NAME"], s3_key, str(video_path))

        # 1. Transcription
        transcript_segments_json = self.transcribe_video(base_dir, video_path)
        transcript_segments = json.loads(transcript_segments_json)

        # 2. Identify moments for clips
        print("Identifying clip moments")
        identified_moments_raw = self.identify_moments(transcript_segments)

        cleaned_json_string = identified_moments_raw.strip()
        if cleaned_json_string.startswith("```json"):
            cleaned_json_string = cleaned_json_string[len("```json"):].strip()
        if cleaned_json_string.endswith("```"):
            cleaned_json_string = cleaned_json_string[:-len("```")].strip()

        clip_moments = json.loads(cleaned_json_string)
        if not isinstance(clip_moments, list):
            print("Error: Identified moments is not a list")
            clip_moments = []
        elif len(clip_moments) == 0:
            print("No clip-worthy moments identified in this video")

        print(clip_moments)

        # 3. Process clips
        for index, moment in enumerate(clip_moments[:5]):
            if "start" in moment and "end" in moment:
                print("Processing clip" + str(index) + " from " +
                      str(moment["start"]) + " to " + str(moment["end"]))
                process_clip(base_dir, video_path, s3_key,
                             moment["start"], moment["end"], index, transcript_segments)

        if base_dir.exists():
            print(f"Cleaning up temp dir after {base_dir}")
            shutil.rmtree(base_dir, ignore_errors=True)


@app.function(
    timeout=1800,
    secrets=[modal.Secret.from_name("ai-podcast-clipper-secret")],
)
@modal.fastapi_endpoint(method="POST")
def download_youtube(request: DownloadYouTubeRequest, token: HTTPAuthorizationCredentials = Depends(auth_scheme)):
    """Download a YouTube video server-side and upload it to S3 under the
    caller-provided key, so the rest of the pipeline (Inngest -> process_video)
    works identically to a browser file upload. Runs on CPU - no GPU billing
    for downloads. Called by the frontend's Inngest function via step.fetch,
    which tolerates long downloads that would blow Vercel's serverless timeout.
    """
    import yt_dlp

    if token.credentials != os.environ["AUTH_TOKEN"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Incorrect bearer token", headers={"WWW-Authenticate": "Bearer"})

    run_id = str(uuid.uuid4())
    base_dir = pathlib.Path("/tmp") / run_id
    base_dir.mkdir(parents=True, exist_ok=True)
    output_path = base_dir / "original.mp4"

    ydl_opts = {
        # Cap at 1080p to bound file size/processing time; merge best video+audio
        # into a single mp4 (ffmpeg is present in the image for the merge step).
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(output_path),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # YouTube bot-checks datacenter IPs on the default web client ("Sign in
        # to confirm you're not a bot"). The TV/Android player clients are not
        # subject to the same check and usually work from cloud IPs.
        "extractor_args": {"youtube": {"player_client": ["tv", "android", "web"]}},
    }

    # If a YOUTUBE_COOKIES secret is configured (Netscape cookies.txt format,
    # exported from a logged-in browser), use it - this is the reliable way to
    # get past YouTube's datacenter-IP bot checks.
    cookies_data = os.environ.get("YOUTUBE_COOKIES")
    if cookies_data:
        cookies_path = base_dir / "cookies.txt"
        cookies_path.write_text(cookies_data)
        ydl_opts["cookiefile"] = str(cookies_path)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(request.url, download=True)
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(base_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"YouTube download failed: {str(e)[:500]}")

    if not output_path.exists():
        shutil.rmtree(base_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Download completed but no output file was produced")

    try:
        s3_client = boto3.client("s3")
        s3_client.upload_file(str(output_path), os.environ["S3_BUCKET_NAME"], request.s3_key)
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)

    return {
        "success": True,
        "s3_key": request.s3_key,
        "video_id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
    }


@app.local_entrypoint()
def main():
    import requests

    ai_podcast_clipper = AiPodcastClipper()

    url = ai_podcast_clipper.process_video.web_url

    payload = {
        "s3_key": "test2/mi630min.mp4"
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer 123123"
    }

    response = requests.post(url, json=payload,
                             headers=headers)
    response.raise_for_status()
    result = response.json()
    print(result)

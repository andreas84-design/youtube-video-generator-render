import os
import base64
import subprocess
import tempfile
import json
import requests

from flask import Flask, request, jsonify
from moviepy.editor import ColorClip, VideoFileClip, concatenate_videoclips, AudioFileClip
from moviepy.config import change_settings
from pexelsapi.pexels import Pexels

change_settings({"FFMPEG_BINARY": "ffmpeg"})

app = Flask(__name__)


@app.route('/ffmpeg-test', methods=['GET'])
def ffmpeg_test():
    result = subprocess.run(
        ["ffmpeg", "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    firstline = result.stdout.splitlines()[0] if result.stdout else "no output"
    return jsonify({"ffmpeg_output": firstline})


@app.route('/generate', methods=['POST'])
def generate():
    try:
        data = request.get_json(force=True) or {}

        audiobase64 = data.get("audiobase64")
        script = data.get("script", "")
        audioduration = data.get("audioduration")
        topic = data.get("topic") or script[:60] or "meditation"

        if not audiobase64:
            return jsonify({
                "success": False,
                "error": "audiobase64 mancante o vuoto",
                "videobase64": None,
                "duration": None,
            }), 400

        # 1. Decodifica base64 in MP3 temporaneo
        try:
            audio_bytes = base64.b64decode(audiobase64)
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Decodifica base64 fallita: {str(e)}",
                "videobase64": None,
                "duration": None,
            }), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            f.write(audio_bytes)
            audiopath = f.name

        # 2. Durata reale audio con ffprobe
        ffprobe_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audiopath,
        ]
        probe = subprocess.run(
            ffprobe_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10
        )

        real_duration = None
        try:
            real_duration = float(probe.stdout.strip())
        except Exception:
            pass

        if real_duration is None or real_duration <= 0:
            try:
                real_duration = float(audioduration)
            except (TypeError, ValueError):
                real_duration = 60.0

        final_video_path = None
        pexels_clips_paths = []

        # 3. Prova a creare B-roll Pexels
        try:
            api_key = os.environ.get("PEXELS_API_KEY")
            print("PEXELS_API_KEY_PRESENT", bool(api_key), "TOPIC", topic)
            if api_key:
                pexel = Pexels(api_key)

                search = pexel.search_videos(
                    query=topic,
                    orientation="landscape",
                    size="hd",
                    page=1,
                    per_page=10
                )

                videos = search.get("videos", [])

                for vid in videos:
                    video_files = vid.get("video_files", [])
                    best = None
                    for vf in video_files:
                        if vf.get("width", 0) >= 1920 and vf.get("height", 0) >= 1080:
                            best = vf
                            break
                    if not best and video_files:
                        best = video_files[0]

                    if not best:
                        continue

                    url = best.get("link")
                    if not url:
                        continue

                    r = requests.get(url, stream=True, timeout=30)
                    if r.status_code != 200:
                        continue

                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            break
                        tmp.write(chunk)
                    tmp.close()
                    pexels_clips_paths.append(tmp.name)

                if len(pexels_clips_paths) >= 2:
                    clips = []
                    for path in pexels_clips_paths:
                        clip = VideoFileClip(path).resize((1920, 1080))
                        clips.append(clip)

                    assembled = []
                    total = 0
                    idx = 0
                    while total < real_duration and clips:
                        c = clips[idx % len(clips)]
                        remaining = real_duration - total
                        if c.duration > remaining:
                            c = c.subclip(0, remaining)
                        assembled.append(c)
                        total += c.duration
                        idx += 1

                    video_clip = concatenate_videoclips(assembled, method="compose")

                    audio_clip = AudioFileClip(audiopath)
                    video_clip = video_clip.set_audio(audio_clip)

                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
                        final_video_path = vf.name

                    video_clip.write_videofile(
                        final_video_path,
                        fps=25,
                        codec="libx264",
                        audio_codec="aac",
                        verbose=False,
                        logger=None,
                    )

                    video_clip.close()
                    audio_clip.close()
                    for c in clips:
                        c.close()

        except Exception:
            pass

        # 4. Fallback: video nero se Pexels non ha prodotto niente
        if not final_video_path:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
                video_mute_path = vf.name

            videoclip = ColorClip(size=(1920, 1080), color=(0, 0, 0))
            videoclip = videoclip.set_duration(real_duration)
            videoclip.write_videofile(
                video_mute_path,
                fps=25,
                codec="libx264",
                audio=False,
                verbose=False,
                logger=None,
            )
            videoclip.close()

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
                final_video_path = vf.name

            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i", video_mute_path,
                "-i", audiopath,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                final_video_path,
            ]

            result = subprocess.run(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                for p in [audiopath, video_mute_path]:
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
                return jsonify({
                    "success": False,
                    "error": f"ffmpeg muxing fallito: {result.stderr[:500]}",
                    "videobase64": None,
                    "duration": None,
                }), 400

            try:
                os.unlink(video_mute_path)
            except Exception:
                pass

        # 5. Leggi video finale e converti in base64
        with open(final_video_path, "rb") as f:
            videobytes = f.read()

        videob64 = base64.b64encode(videobytes).decode("utf-8")

        # 6. Cleanup
        for p in [audiopath, final_video_path] + pexels_clips_paths:
            try:
                os.unlink(p)
            except Exception:
                pass

        return jsonify({
            "success": True,
            "error": None,
            "videobase64": videob64,
            "duration": real_duration,
        }), 200

    except subprocess.TimeoutExpired as e:
        return jsonify({
            "success": False,
            "error": f"Timeout durante elaborazione: {str(e)}",
            "videobase64": None,
            "duration": None,
        }), 500
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "videobase64": None,
            "duration": None,
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)

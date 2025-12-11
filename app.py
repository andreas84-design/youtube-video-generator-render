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
    
@app.route('/pexels-test', methods=['GET'])
def pexels_test():
    try:
        topic = request.args.get("topic", "meditation")
        api_key = os.environ.get("PEXELS_API_KEY")
        print("PEXELS_TEST_KEY_PRESENT", bool(api_key), "TOPIC", topic)

        if not api_key:
            return jsonify({"ok": False, "error": "No PEXELS_API_KEY"}), 500

        pexel = Pexels(api_key)
        search = pexel.search_videos(
            query=topic,
            orientation="landscape",
            size="hd",
            page=1,
            per_page=5
        )
        videos = search.get("videos", [])
        print("PEXELS_TEST_VIDEOS_FOUND", len(videos))

        return jsonify({
            "ok": True,
            "topic": topic,
            "videos_found": len(videos),
        }), 200
    except Exception as e:
        print("PEXELS_TEST_ERROR", str(e))
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/test-pexels-video', methods=['GET'])
def test_pexels_video():
    try:
        topic = request.args.get("topic", "meditation")
        api_key = os.environ.get("PEXELS_API_KEY")
        
        if not api_key:
            return jsonify({"ok": False, "error": "No PEXELS_API_KEY"}), 500

        pexel = Pexels(api_key)
        search = pexel.search_videos(
            query=topic,
            orientation="landscape",
            size="hd",
            page=1,
            per_page=5
        )
        videos = search.get("videos", [])
        
        if len(videos) == 0:
            return jsonify({"ok": False, "error": "No videos found"}), 200

        # Scarica prima clip
        vid = videos[0]
        video_files = vid.get("video_files", [])
        best = None
        for vf in video_files:
            if vf.get("width", 0) >= 1920 and vf.get("height", 0) >= 1080:
                best = vf
                break
        if not best and video_files:
            best = video_files[0]

        if not best:
            return jsonify({"ok": False, "error": "No valid video file"}), 200

        url = best.get("link")
        r = requests.get(url, stream=True, timeout=30)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"Download failed: {r.status_code}"}), 200

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                break
            tmp.write(chunk)
        tmp.close()

        # Prova a caricare con MoviePy
        clip = VideoFileClip(tmp.name)
        duration = clip.duration
        width = clip.w
        height = clip.h
        clip.close()

        os.unlink(tmp.name)

        return jsonify({
            "ok": True,
            "topic": topic,
            "duration": duration,
            "width": width,
            "height": height,
        }), 200

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
        pexels_clip_path = None

        # 3. Prova Pexels: UNA sola clip, come nel test
        try:
            api_key = os.environ.get("PEXELS_API_KEY")
            print("GENERATE_PEXELS_KEY_PRESENT", bool(api_key), "TOPIC", topic)

            if api_key:
                pexel = Pexels(api_key)
                search = pexel.search_videos(
                    query=topic,
                    orientation="landscape",
                    size="hd",
                    page=1,
                    per_page=5
                )
                videos = search.get("videos", [])
                print("GENERATE_PEXELS_VIDEOS_FOUND", len(videos))

                if len(videos) > 0:
                    vid = videos[0]
                    video_files = vid.get("video_files", [])
                    best = None
                    for vf in video_files:
                        if vf.get("width", 0) >= 1920 and vf.get("height", 0) >= 1080:
                            best = vf
                            break
                    if not best and video_files:
                        best = video_files[0]

                    if best:
                        url = best.get("link")
                        if url:
                            r = requests.get(url, stream=True, timeout=30)
                            if r.status_code == 200:
                                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                                for chunk in r.iter_content(chunk_size=1024 * 1024):
                                    if not chunk:
                                        break
                                    tmp.write(chunk)
                                tmp.close()
                                pexels_clip_path = tmp.name
                                print("GENERATE_USING_PEXELS_CLIP", pexels_clip_path)

        except Exception as e:
            print("GENERATE_PEXELS_ERROR", str(e))
            pexels_clip_path = None

        # 4. Se abbiamo una clip Pexels, creiamo il video con quella
        if pexels_clip_path:
            try:
                video_clip = VideoFileClip(pexels_clip_path).resize((1920, 1080))

                if video_clip.duration > real_duration:
                    video_clip = video_clip.subclip(0, real_duration)

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

            except Exception as e:
                print("GENERATE_MOVIEPY_ERROR", str(e))
                final_video_path = None

                # 5. Se final_video_path è ancora None, restituiamo errore (niente fallback nero)
        if not final_video_path:
            # cleanup base
            try:
                os.unlink(audiopath)
            except Exception:
                pass
            if pexels_clip_path:
                try:
                    os.unlink(pexels_clip_path)
                except Exception:
                    pass

            return jsonify({
                "success": False,
                "error": "Nessuna clip Pexels usata: final_video_path è None",
                "videobase64": None,
                "duration": None,
            }), 500


            try:
                os.unlink(video_mute_path)
            except Exception:
                pass

        # 6. Leggi video finale e converti in base64
        with open(final_video_path, "rb") as f:
            videobytes = f.read()

        videob64 = base64.b64encode(videobytes).decode("utf-8")

        # 7. Cleanup
        paths_to_clean = [audiopath, final_video_path]
        if pexels_clip_path:
            paths_to_clean.append(pexels_clip_path)
        for p in paths_to_clean:
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

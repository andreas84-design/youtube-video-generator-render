import os
import base64
import subprocess
import tempfile
import json
from flask import Flask, request, jsonify
from moviepy.editor import ColorClip
from moviepy.config import change_settings

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

        # 2. Leggi la durata reale dell'MP3 con ffprobe
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
            timeout=10  # timeout breve per evitare blocchi
        )

        real_duration = None
        try:
            real_duration = float(probe.stdout.strip())
        except Exception:
            pass

        # Fallback
        if real_duration is None or real_duration <= 0:
            try:
                real_duration = float(audioduration)
            except (TypeError, ValueError):
                real_duration = 60.0

        # 3. Crea video muto nero 1920x1080 con durata = real_duration
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

        # 4. Usa ffmpeg per aggiungere l'audio al video muto
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
            timeout=300  # timeout di sicurezza per ffmpeg
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

        # 5. Leggi video finale e converti in base64
        with open(final_video_path, "rb") as f:
            videobytes = f.read()

        videob64 = base64.b64encode(videobytes).decode("utf-8")

        # 6. Cleanup
        for p in [audiopath, video_mute_path, final_video_path]:
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

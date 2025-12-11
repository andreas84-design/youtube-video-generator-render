import os
import base64
import subprocess
import tempfile
from flask import Flask, request, jsonify
from moviepy.editor import ColorClip
from moviepy.config import change_settings

# Usa ffmpeg di sistema
change_settings({"FFMPEG_BINARY": "ffmpeg"})

app = Flask(__name__)


@app.route('/ffmpeg-test', methods=['GET'])
def ffmpeg_test():
    """Endpoint diagnostico per verificare versione ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    firstline = result.stdout.splitlines()[0] if result.stdout else "no output"
    return jsonify({"ffmpeg_output": firstline})


@app.route('/generate', methods=['POST'])
def generate():
    """
    Body JSON atteso:
    {
        "audiobase64": "...",   # audio MP3 in base64 da Google TTS
        "script": "...",
        "audioduration": 90.5   # durata in secondi calcolata in n8n
    }
    """
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
                "duration": None
            }), 400

        try:
            real_duration = float(audioduration)
        except (TypeError, ValueError):
            real_duration = 60.0

        # 1. Decodifica base64 in MP3 temporaneo
        try:
            audio_bytes = base64.b64decode(audiobase64)
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Decodifica base64 fallita: {str(e)}",
                "videobase64": None,
                "duration": None
            }), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            f.write(audio_bytes)
            audiopath = f.name

        # 2. Crea video muto nero 1920x1080 con durata passata
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
            logger=None
        )
        videoclip.close()

        # 3. Usa ffmpeg per aggiungere l'audio al video muto
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
            final_video_path
        ]

        result = subprocess.run(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:
            try:
                os.unlink(audiopath)
                os.unlink(video_mute_path)
            except Exception:
                pass
            return jsonify({
                "success": False,
                "error": f"ffmpeg muxing fallito: {result.stderr[:500]}",
                "videobase64": None,
                "duration": None
            }), 400

        # 4. Leggi video finale e converti in base64
        with open(final_video_path, "rb") as f:
            videobytes = f.read()

        videob64 = base64.b64encode(videobytes).decode("utf-8")

        # 5. Cleanup
        for p in [audiopath, video_mute_path, final_video_path]:
            try:
                os.unlink(p)
            except Exception:
                pass

        return jsonify({
            "success": True,
            "error": None,
            "videobase64": videob64,
            "duration": real_duration
        }), 200

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "videobase64": None,
            "duration": None
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)

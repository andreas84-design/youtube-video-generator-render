import os
import base64
import subprocess
import tempfile
import requests
from flask import Flask, request, jsonify
from moviepy.editor import ColorClip
from moviepy.config import change_settings

change_settings({"FFMPEG_BINARY": "ffmpeg"})

app = Flask(__name__)


@app.route('/ffmpeg-test', methods=['GET'])
def ffmpeg_test():
    """Endpoint diagnostico per verificare versione ffmpeg."""
    result = subprocess.run(
        ['ffmpeg', '-version'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    firstline = result.stdout.splitlines()[0] if result.stdout else "no output"
    return jsonify({"ffmpeg_output": firstline})


@app.route('/generate', methods=['POST'])
def generate():
    """
    Endpoint principale chiamato da n8n.

    Body JSON:
    {
        "audiourl": "https://drive.google.com/uc?export=download&id=...",
        "script": "...",
        "audioduration": 90.5
    }

    Risposta:
    {
        "success": true/false,
        "videobase64": "...",
        "duration": 90.5,
        "error": null
    }
    """
    try:
        # 1. Parsing input
        data = request.get_json(force=True) or {}
        audiourl = data.get("audiourl")
        script = data.get("script", "")
        audioduration = data.get("audioduration")

        if not audiourl:
            return jsonify({
                "success": False,
                "error": "audiourl mancante o vuoto",
                "videobase64": None,
                "duration": None
            }), 400

        try:
            real_duration = float(audioduration)
        except (TypeError, ValueError):
            real_duration = 60.0

        # 2. Scarica MP3 da audiourl
        resp = requests.get(audiourl, timeout=120)
        if resp.status_code != 200:
            return jsonify({
                "success": False,
                "error": f"Download audio fallito: status {resp.status_code}",
                "videobase64": None,
                "duration": None
            }), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            f.write(resp.content)
            audiopath = f.name

        # 3. Crea video muto nero 1920x1080 con durata passata
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            video_mute_path = vf.name

        videoclip = ColorClip(size=(1920, 1080), color=(0, 0, 0))
        videoclip = videoclip.set_duration(real_duration)
        videoclip.write_videofile(
            video_mute_path,
            fps=25,
            codec="libx264",
            audio=False,  # Nessun audio qui
            verbose=False,
            logger=None
        )
        videoclip.close()

        # 4. Usa ffmpeg per aggiungere l'audio al video muto
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            final_video_path = vf.name

        ffmpeg_cmd = [
            "ffmpeg",
            "-y",  # Sovrascrivi output
            "-i", video_mute_path,  # Video muto
            "-i", audiopath,         # Audio MP3
            "-c:v", "copy",          # Copia video senza re-encode
            "-c:a", "aac",           # Audio codec AAC
            "-shortest",             # Durata = minore tra video e audio
            final_video_path
        ]

        result = subprocess.run(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:
            # ffmpeg fallito
            os.unlink(audiopath)
            os.unlink(video_mute_path)
            return jsonify({
                "success": False,
                "error": f"ffmpeg muxing fallito: {result.stderr[:500]}",
                "videobase64": None,
                "duration": None
            }), 400

        # 5. Leggi video finale e converti in base64
        with open(final_video_path, "rb") as f:
            videobytes = f.read()

        videob64 = base64.b64encode(videobytes).decode("utf-8")

        # 6. Cleanup
        try:
            os.unlink(audiopath)
        except Exception:
            pass

        try:
            os.unlink(video_mute_path)
        except Exception:
            pass

        try:
            os.unlink(final_video_path)
        except Exception:
            pass

        # 7. Risposta OK
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

import os
import base64
import subprocess
import tempfile
import requests
from flask import Flask, request, jsonify
from moviepy.editor import ColorClip, AudioFileClip
from moviepy.config import change_settings

# Usa ffmpeg di sistema installato nel Dockerfile
change_settings({"FFMPEG_BINARY": "ffmpeg"})

app = Flask(__name__)


@app.route('/ffmpeg-test', methods=['GET'])
def ffmpeg_test():
    """Endpoint diagnostico per verificare la versione di ffmpeg nel container."""
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

    Body JSON atteso:
    {
        "audiourl": "https://drive.google.com/uc?export=download&id=...",
        "script": "Testo script completo...",
        "audioduration": 90.5    # durata in secondi calcolata in n8n
    }

    Risposta:
    {
        "success": true/false,
        "videobase64": "...",    # MP4 base64
        "duration": 90.5,
        "error": "messaggio opzionale"
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

        # Durata: usa il valore passato da n8n (fallback 60s)
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

        # 3. Carica audio con MoviePy SENZA leggerne la durata
        try:
            audioclip = AudioFileClip(audiopath)
        except Exception as e:
            os.unlink(audiopath)
            return jsonify({
                "success": False,
                "error": f"MoviePy non riesce a usare audio: {str(e)}",
                "videobase64": None,
                "duration": None
            }), 400

        # 4. Crea video nero 1920x1080 con durata = real_duration e audio TTS
        videoclip = ColorClip(size=(1920, 1080), color=(0, 0, 0))
        videoclip = videoclip.set_duration(real_duration)
        videoclip = videoclip.set_audio(audioclip)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            videopath = vf.name
            videoclip.write_videofile(
                videopath,
                fps=25,
                codec="libx264",
                audio_codec="aac",
                verbose=False,
                logger=None
            )

        # 5. Leggi MP4 e converti in base64
        with open(videopath, "rb") as f:
            videobytes = f.read()

        videob64 = base64.b64encode(videobytes).decode("utf-8")

        # 6. Cleanup
        try:
            audioclip.close()
            videoclip.close()
        except Exception:
            pass

        try:
            os.unlink(audiopath)
        except Exception:
            pass

        try:
            os.unlink(videopath)
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
        # Errore imprevisto
        return jsonify({
            "success": False,
            "error": str(e),
            "videobase64": None,
            "duration": None
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)

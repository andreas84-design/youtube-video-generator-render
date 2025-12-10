import os
import base64
import subprocess
import tempfile

import requests
from flask import Flask, request, jsonify
from moviepy.editor import ColorClip
from moviepy.config import change_settings

# Usa ffmpeg di sistema
change_settings({"FFMPEG_BINARY": "ffmpeg"})

app = Flask(__name__)


@app.get("/ffmpeg-test")
def ffmpeg_test():
    """
    Endpoint di diagnostica per verificare la versione di ffmpeg nel container.
    """
    result = subprocess.run(
        ["ffmpeg", "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    first_line = result.stdout.splitlines()[0] if result.stdout else "no output"
    return jsonify({"ffmpeg_output": first_line})


@app.post("/generate")
def generate():
    """
    Endpoint principale chiamato da n8n.

    Body JSON atteso:
    {
      "audio_url": "https://.....mp3",
      "script": "testo completo...",
      "audio_duration": 123.45   # opzionale, in secondi
    }

    Risposta:
    {
      "success": true/false,
      "video_base64": "...",
      "duration": 123.45,
      "error": "messaggio opzionale"
    }
    """
    try:
        data = request.get_json(force=True) or {}
        audio_url = data.get("audio_url")
        script = data.get("script", "")
        audio_duration = data.get("audio_duration")

        if not audio_url:
            return jsonify({
                "success": False,
                "error": "audio_url mancante o vuoto",
                "video_base64": None,
                "duration": None,
            }), 400

        # Se audio_duration non è passato o non è valido, usa un fallback
        try:
            audio_duration = float(audio_duration)
        except (TypeError, ValueError):
            audio_duration = 60.0  # fallback di sicurezza

        # 1) Scarica l'MP3 da audio_url
        resp = requests.get(audio_url, timeout=120)
        if resp.status_code != 200:
            return jsonify({
                "success": False,
                "error": f"Download audio fallito (status {resp.status_code})",
                "video_base64": None,
                "duration": None,
            }), 400

        # Salva audio in un file temporaneo (solo per logging / debug futuro)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            f.write(resp.content)
            audio_path = f.name

        # 2) CREA UN VIDEO SEMPLICE MUTO (sfondo nero) CON LA DURATA PASSATA
        video_clip = ColorClip(size=(1920, 1080), color=(0, 0, 0))
        video_clip = video_clip.set_duration(audio_duration)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            video_path = vf.name

        video_clip.write_videofile(
            video_path,
            fps=25,
            codec="libx264",
            audio=False,
            verbose=False,
            logger=None,
        )

        video_clip.close()
        os.unlink(audio_path)

        # 3) Leggi il video e convertilo in base64
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        os.unlink(video_path)

        video_b64 = base64.b64encode(video_bytes).decode("utf-8")

        return jsonify({
            "success": True,
            "error": None,
            "video_base64": video_b64,
            "duration": audio_duration,
        }), 200

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "video_base64": None,
            "duration": None,
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)

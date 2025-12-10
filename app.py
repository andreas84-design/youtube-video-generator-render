import os
import io
import base64
import subprocess
import tempfile

from flask import Flask, request, jsonify
from moviepy.editor import AudioFileClip  # + il tuo VideoGenerator, se lo usi


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
      "script": "testo completo..."
    }
    Ritorna:
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

        if not audio_url:
            return jsonify({
                "success": False,
                "error": "audio_url mancante o vuoto",
                "video_base64": None,
                "duration": None
            }), 400

        # 1) Scarica l'MP3 da audio_url
        import requests

        resp = requests.get(audio_url, timeout=60)
        if resp.status_code != 200:
            return jsonify({
                "success": False,
                "error": f"Download audio fallito (status {resp.status_code})",
                "video_base64": None,
                "duration": None
            }), 400

        # Salva audio in un file temporaneo
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            f.write(resp.content)
            audio_path = f.name

        # 2) Controllo rapido con ffmpeg: deve avere Duration
        ffprobe = subprocess.run(
            ["ffmpeg", "-i", audio_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log = ffprobe.stdout or ""
        if "Duration:" not in log:
            size = os.path.getsize(audio_path)
            os.unlink(audio_path)
            return jsonify({
                "success": False,
                "error": f"ffmpeg non trova la Duration nell'audio (size={size} bytes)",
                "video_base64": None,
                "duration": None
            }), 400

        # 3) Leggi audio con MoviePy
        audio_clip = AudioFileClip(audio_path)
        audio_duration = float(audio_clip.duration)

        # TODO: QUI METTI LA TUA LOGICA DI VIDEO:
        # - usare script + audio_clip per costruire il video (B-roll, captions, ecc.)
        # - alla fine devi avere un file MP4 su disco, es. video_path

        # ESEMPIO MINIMO: solo audio su sfondo nero (da sostituire col tuo VideoGenerator)
        from moviepy.editor import ColorClip

        video_clip = ColorClip(size=(1920, 1080), color=(0, 0, 0))
        video_clip = video_clip.set_duration(audio_duration).set_audio(audio_clip)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            video_path = vf.name

        video_clip.write_videofile(
            video_path,
            fps=25,
            codec="libx264",
            audio_codec="aac",
            verbose=False,
            logger=None
        )

        audio_clip.close()
        video_clip.close()
        os.unlink(audio_path)

        # 4) Leggi il video e convertilo in base64
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        os.unlink(video_path)

        video_b64 = base64.b64encode(video_bytes).decode("utf-8")

        return jsonify({
            "success": True,
            "error": None,
            "video_base64": video_b64,
            "duration": audio_duration
        }), 200

    except Exception as e:
        # In caso di errore imprevisto
        return jsonify({
            "success": False,
            "error": str(e),
            "video_base64": None,
            "duration": None
        }), 500


if __name__ == "__main__":
    # Solo per debug locale; in Railway usiamo gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)

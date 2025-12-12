import os
import base64
import subprocess
import tempfile
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/ffmpeg-test', methods=['GET'])
def ffmpeg_test():
    """Endpoint diagnostico per verificare versione ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    firstline = result.stdout.splitlines()[0] if result.stdout else "no output"
    return jsonify({"ffmpeg_output": firstline})

@app.route('/test-pexels', methods=['GET'])
def test_pexels():
    """Test connessione API Pexels."""
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        return jsonify({"success": False, "error": "PEXELS_API_KEY non configurata"}), 500

    try:
        headers = {"Authorization": api_key}
        response = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params={"query": "meditation nature", "orientation": "landscape", "per_page": 3},
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        videos = data.get("videos", [])

        return jsonify({
            "success": True,
            "api_key_configured": True,
            "videos_found": len(videos),
            "first_video_id": videos[0]["id"] if videos else None
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/generate', methods=['POST'])
def generate():
    """
    Body JSON atteso da n8n:
    {
        "audiobase64": "...",      # audio in base64 (Google TTS)
        "script": "...",           # testo script completo
        "audioduration": 180.0,    # durata stimata (fallback)
        "broll_keywords": "woman walking, healthy breakfast"  # keywords per Pexels
    }
    """
    audiopath = None
    audio_wav_path = None
    pexels_clip_path = None
    video_looped_path = None
    final_video_path = None

    try:
        data = request.get_json(force=True) or {}
        audiobase64 = data.get("audiobase64")
        script = data.get("script", "")
        audioduration = data.get("audioduration")
        broll_keywords = data.get("broll_keywords", "").strip()

        if not audiobase64:
            return jsonify({
                "success": False,
                "error": "audiobase64 mancante o vuoto",
                "videobase64": None,
                "duration": None,
            }), 400

        # Fallback: topic da prime parole dello script
        if not broll_keywords:
            words = script.split()[:8]
            broll_keywords = " ".join(words) if words else "wellness meditation"

        query_keywords = broll_keywords.split(",")
        pexels_query = query_keywords[0].strip() if query_keywords else broll_keywords

        # 1. Decodifica base64 in file temporaneo
        try:
            audio_bytes = base64.b64decode(audiobase64)
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Decodifica base64 fallita: {str(e)}",
                "videobase64": None,
                "duration": None,
            }), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(audio_bytes)
            audiopath = f.name

        # 2. Converti audio in WAV per compatibilità universale
        audio_wav_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        audio_wav_path = audio_wav_tmp.name
        audio_wav_tmp.close()

        convert_audio_cmd = [
            "ffmpeg", "-y",
            "-i", audiopath,
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            audio_wav_path,
        ]
        conv_result = subprocess.run(
            convert_audio_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if conv_result.returncode != 0:
            raise Exception(f"Conversione audio fallita: {conv_result.stderr}")

        # Elimina il file .bin originale e usa il WAV
        os.unlink(audiopath)
        audiopath = audio_wav_path

        # 3. Durata reale dell'audio con ffprobe
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

        # 4. Scarica clip Pexels (REST API)
        api_key = os.environ.get("PEXELS_API_KEY")
        if not api_key:
            return jsonify({
                "success": False,
                "error": "PEXELS_API_KEY non configurata in Railway",
                "videobase64": None,
                "duration": None,
            }), 500

        headers = {"Authorization": api_key}
        search_params = {
            "query": pexels_query,
            "orientation": "landscape",
            "per_page": 5
        }

        search_response = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params=search_params,
            timeout=30
        )
        search_response.raise_for_status()
        search_data = search_response.json()
        videos = search_data.get("videos", [])

        if not videos:
            return jsonify({
                "success": False,
                "error": f"Nessun video Pexels trovato per query: '{pexels_query}'",
                "videobase64": None,
                "duration": None,
            }), 500

        video_files = videos[0].get("video_files", [])
        if not video_files:
            return jsonify({
                "success": False,
                "error": "Nessun file video disponibile nel risultato Pexels",
                "videobase64": None,
                "duration": None,
            }), 500

        # Scegli versione HD se possibile
        hd_files = [vf for vf in video_files if vf.get("width", 0) >= 1920]
        if hd_files:
            best_video = max(hd_files, key=lambda x: x.get("width", 0))
        else:
            best_video = max(video_files, key=lambda x: x.get("width", 0))

        video_url = best_video.get("link")
        if not video_url:
            return jsonify({
                "success": False,
                "error": "URL video Pexels non disponibile",
                "videobase64": None,
                "duration": None,
            }), 500

        # 5. Scarica video Pexels
        r = requests.get(video_url, stream=True, timeout=120)
        r.raise_for_status()

        pexels_clip_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                pexels_clip_tmp.write(chunk)
        pexels_clip_tmp.close()
        pexels_clip_path = pexels_clip_tmp.name

        # 6. Durata clip Pexels
        probe_clip = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", pexels_clip_path],
            stdout=subprocess.PIPE,
            text=True,
            timeout=10
        )

        clip_duration = 10.0
        try:
            clip_duration = float(probe_clip.stdout.strip())
        except Exception:
            pass

        # 7. Loop / trim video Pexels con ffmpeg concat
        video_looped_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_looped_path = video_looped_tmp.name
        video_looped_tmp.close()

        if clip_duration < real_duration:
            loops = int(real_duration / clip_duration) + 1
            concat_list_tmp = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".txt")
            for _ in range(loops):
                concat_list_tmp.write(f"file '{pexels_clip_path}'\n")
            concat_list_tmp.close()

            concat_cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list_tmp.name,
                "-c", "copy",
                "-t", str(real_duration),
                video_looped_path
            ]
            subprocess.run(concat_cmd, timeout=180, check=True)
            os.unlink(concat_list_tmp.name)
        else:
            trim_cmd = [
                "ffmpeg", "-y",
                "-i", pexels_clip_path,
                "-t", str(real_duration),
                "-c", "copy",
                video_looped_path
            ]
            subprocess.run(trim_cmd, timeout=180, check=True)

        # 8. Resize a 1920x1080 e aggiungi audio WAV
        final_video_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        final_video_path = final_video_tmp.name
        final_video_tmp.close()

        merge_cmd = [
            "ffmpeg", "-y",
            "-i", video_looped_path,
            "-i", audiopath,
            "-filter:v",
            "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            final_video_path,
        ]

        result = subprocess.run(
            merge_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise Exception(f"ffmpeg merge fallito: {result.stderr}")

        # 9. Leggi video finale e converti in base64
        with open(final_video_path, "rb") as f:
            videobytes = f.read()
        videob64 = base64.b64encode(videobytes).decode("utf-8")

        # 10. Cleanup
        for p in [audiopath, pexels_clip_path, video_looped_path, final_video_path]:
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass

        return jsonify({
            "success": True,
            "error": None,
            "videobase64": videob64,
            "duration": real_duration,
            "pexels_query": pexels_query,
            "pexels_video_id": videos[0].get("id")
        }), 200

    except subprocess.TimeoutExpired as e:
        for p in [audiopath, audio_wav_path, pexels_clip_path, video_looped_path, final_video_path]:
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass
        return jsonify({
            "success": False,
            "error": f"Timeout durante elaborazione: {str(e)}",
            "videobase64": None,
            "duration": None,
        }), 500

    except Exception as e:
        for p in [audiopath, audio_wav_path, pexels_clip_path, video_looped_path, final_video_path]:
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass
        return jsonify({
            "success": False,
            "error": str(e),
            "videobase64": None,
            "duration": None,
        }), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)

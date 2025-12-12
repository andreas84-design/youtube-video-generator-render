import os
import base64
import json
import tempfile
import subprocess
import uuid
import datetime as dt

import requests
from flask import Flask, request, jsonify
import boto3
from botocore.config import Config

app = Flask(__name__)

# Config R2 (S3 compatibile)
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL")  # es: https://pub-....r2.dev
R2_REGION = os.environ.get("R2_REGION", "auto")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")  # opzionale, se vuoi usare endpoint account-scoped


def get_s3_client():
    """
    Client S3 configurato per Cloudflare R2.
    Usa l'endpoint account-scoped se R2_ACCOUNT_ID è presente,
    altrimenti l'endpoint pubblico base.
    """
    if R2_ACCOUNT_ID:
        endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    else:
        # fallback: prova a ricavare l'account ID dall'URL pubblico se è del tipo pub-xxx.r2.dev
        endpoint_url = None
        if ".r2.dev" in (R2_PUBLIC_BASE_URL or ""):
            # per R2 S3 API serve comunque l'endpoint account-scoped,
            # quindi è meglio impostare R2_ACCOUNT_ID in Railway se puoi.
            pass

    if endpoint_url is None:
        raise RuntimeError("Endpoint R2 non configurato: imposta R2_ACCOUNT_ID in Railway")

    session = boto3.session.Session()
    s3_client = session.client(
        service_name="s3",
        region_name=R2_REGION,  # per R2 deve essere 'auto' o vuoto [web:90][web:95]
        endpoint_url=endpoint_url,  # https://<ACCOUNT_ID>.r2.cloudflarestorage.com [web:90][web:91]
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(s3={"addressing_style": "virtual"}),
    )
    return s3_client


@app.route("/ffmpeg-test", methods=["GET"])
def ffmpeg_test():
    result = subprocess.run(
        ["ffmpeg", "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    firstline = result.stdout.splitlines()[0] if result.stdout else "no output"
    return jsonify({"ffmpeg_output": firstline})


@app.route("/generate", methods=["POST"])
def generate():
    audiopath = None
    audio_wav_path = None
    pexels_clip_path = None
    video_looped_path = None
    final_video_path = None

    try:
        # Controllo config R2
        if not all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL]):
            return jsonify({
                "success": False,
                "error": "Config R2 mancante (chiavi, bucket o URL pubblico).",
                "video_url": None,
                "duration": None,
            }), 500

        data = request.get_json(force=True) or {}
        audiobase64 = data.get("audiobase64")
        script = data.get("script", "")
        audioduration = data.get("audioduration")
        broll_keywords = data.get("broll_keywords", "").strip()

        if not audiobase64:
            return jsonify({
                "success": False,
                "error": "audiobase64 mancante o vuoto",
                "video_url": None,
                "duration": None,
            }), 400

        if not broll_keywords:
            words = script.split()[:8]
            broll_keywords = " ".join(words) if words else "wellness meditation"

        query_keywords = broll_keywords.split(",")
        pexels_query = query_keywords[0].strip() if query_keywords else broll_keywords

        # 1. decode audio base64 -> temp .bin
        try:
            audio_bytes = base64.b64decode(audiobase64)
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Decodifica base64 fallita: {str(e)}",
                "video_url": None,
                "duration": None,
            }), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(audio_bytes)
            audiopath = f.name

        # 2. convert audio to WAV 48kHz
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

        os.unlink(audiopath)
        audiopath = audio_wav_path

        # 3. real duration from WAV
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
            timeout=10,
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

        # 4. Pexels search
        api_key = os.environ.get("PEXELS_API_KEY")
        if not api_key:
            return jsonify({
                "success": False,
                "error": "PEXELS_API_KEY non configurata in Railway",
                "video_url": None,
                "duration": None,
            }), 500

        headers = {"Authorization": api_key}
        search_params = {
            "query": pexels_query,
            "orientation": "landscape",
            "per_page": 5,
        }
        search_response = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params=search_params,
            timeout=30,
        )
        search_response.raise_for_status()
        search_data = search_response.json()
        videos = search_data.get("videos", [])

        if not videos:
            return jsonify({
                "success": False,
                "error": f"Nessun video Pexels trovato per query: '{pexels_query}'",
                "video_url": None,
                "duration": None,
            }), 500

        video_files = videos[0].get("video_files", [])
        if not video_files:
            return jsonify({
                "success": False,
                "error": "Nessun file video disponibile nel risultato Pexels",
                "video_url": None,
                "duration": None,
            }), 500

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
                "video_url": None,
                "duration": None,
            }), 500

        # 5. download pexels video
        r = requests.get(video_url, stream=True, timeout=120)
        r.raise_for_status()
        pexels_clip_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                pexels_clip_tmp.write(chunk)
        pexels_clip_tmp.close()
        pexels_clip_path = pexels_clip_tmp.name

        # 6. get clip duration
        probe_clip = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", pexels_clip_path],
            stdout=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        clip_duration = 10.0
        try:
            clip_duration = float(probe_clip.stdout.strip())
        except Exception:
            pass

        # 7. loop / trim video
        video_looped_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_looped_path = video_looped_tmp.name
        video_looped_tmp.close()

        if clip_duration < real_duration:
            loops = int(real_duration / clip_duration) + 1
            concat_list_tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
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
                video_looped_path,
            ]
            subprocess.run(concat_cmd, timeout=180, check=True)
            os.unlink(concat_list_tmp.name)
        else:
            trim_cmd = [
                "ffmpeg", "-y",
                "-i", pexels_clip_path,
                "-t", str(real_duration),
                "-c", "copy",
                video_looped_path,
            ]
            subprocess.run(trim_cmd, timeout=180, check=True)

        # 8. final merge video+audio
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

        # 9. upload su R2
        s3_client = get_s3_client()

        # object key unico: es. videos/2025-12-12/UUID.mp4
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        object_key = f"videos/{today}/{uuid.uuid4().hex}.mp4"

        s3_client.upload_file(
            Filename=final_video_path,
            Bucket=R2_BUCKET_NAME,
            Key=object_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )  # [web:73][web:79][web:91]

        # URL pubblico finale: base + / + object_key
        # es: https://pub-xxx.r2.dev/videos/2025-12-12/uuid.mp4
        public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"

        # cleanup locali
        for p in [audiopath, audio_wav_path, pexels_clip_path, video_looped_path, final_video_path]:
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass

        return jsonify({
            "success": True,
            "error": None,
            "video_url": public_url,
            "duration": real_duration,
        })

    except Exception as e:
        for p in [audiopath, audio_wav_path, pexels_clip_path, video_looped_path, final_video_path]:
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass

        return jsonify({
            "success": False,
            "error": str(e),
            "video_url": None,
            "duration": None,
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

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
from deep_translator import GoogleTranslator

app = Flask(__name__)

# Config R2 (S3 compatibile)
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL")
R2_REGION = os.environ.get("R2_REGION", "auto")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")


def get_s3_client():
    """
    Client S3 configurato per Cloudflare R2.
    """
    if R2_ACCOUNT_ID:
        endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    else:
        endpoint_url = None

    if endpoint_url is None:
        raise RuntimeError("Endpoint R2 non configurato: imposta R2_ACCOUNT_ID in Railway")

    session = boto3.session.Session()
    s3_client = session.client(
        service_name="s3",
        region_name=R2_REGION,
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(s3={"addressing_style": "virtual"}),
    )
    return s3_client


def cleanup_old_videos(s3_client, current_key):
    """
    Cancella tutti i video MP4 in R2 TRANNE quello appena caricato.
    Mantiene solo l'ultimo video pubblicato.
    """
    try:
        # Lista TUTTI gli oggetti nel bucket
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix="videos/")
        
        deleted_count = 0
        for page in pages:
            if 'Contents' not in page:
                continue
                
            for obj in page['Contents']:
                key = obj['Key']
                # Cancella SOLO MP4 che NON sono il video appena caricato
                if key.endswith('.mp4') and key != current_key:
                    s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                    deleted_count += 1
                    print(f"🗑️  Cancellato vecchio video: {key}")
        
        if deleted_count > 0:
            print(f"✅ Rotazione completata: {deleted_count} video vecchi rimossi")
        else:
            print("✅ Nessun video vecchio da rimuovere")
            
    except Exception as e:
        print(f"⚠️  Errore rotazione R2 (video vecchi restano): {str(e)}")


def translate_broll_keywords(keywords_text):
    """Traduce keywords italiane → inglese usando deep-translator (stabile)"""
    if not keywords_text:
        return "women health wellness sleep"
    
    try:
        parts = [p.strip() for p in keywords_text.split(",") if p.strip()]
        
        translated = []
        for part in parts:
            result = GoogleTranslator(source='it', target='en').translate(part)
            translated.append(result.lower())
        
        broll_query = " ".join(translated)
        print(f"🌐 Traduzione keywords: '{keywords_text}' → '{broll_query}'")
        return broll_query[:100]
        
    except Exception as e:
        print(f"⚠️ Errore traduzione: {e}")
        # Fallback dizionario base
        fallback_map = {
            "donne": "women",
            "menopausa": "menopause",
            "vampate": "hot flashes",
            "vampate di calore": "hot flashes",
            "sonno": "sleep",
            "insonnia": "insomnia",
            "dimagrimento": "weight loss",
            "pancia gonfia": "bloating",
            "articolazioni": "joints",
            "dolore": "pain",
            "ginocchia": "knees",
            "benessere": "wellness",
            "salute": "health",
        }
        parts = [p.strip().lower() for p in keywords_text.split(",")]
        translated = [fallback_map.get(p, p) for p in parts]
        return " ".join(translated)[:100]


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
    video_looped_path = None
    final_video_path = None
    scene_paths = []

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

        # ===============================================
        # PEXELS B-ROLL - Keywords dal foglio tradotte dinamicamente
        # ===============================================
        sheet_keywords = data.get("keywords", "").strip()

        # TRADUCI keywords italiane → inglese
        pexels_query = translate_broll_keywords(sheet_keywords)

        if not audiobase64:
            return jsonify({
                "success": False,
                "error": "audiobase64 mancante o vuoto",
                "video_url": None,
                "duration": None,
            }), 400

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

        # 4. Pexels search MULTI-SCENE
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
            "per_page": 30,
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

        max_scenes = 24
        total_scene_duration = 0.0

        for v in videos:
            if len(scene_paths) >= max_scenes:
                break

            video_files = v.get("video_files", [])
            if not video_files:
                continue

            hd_files = [vf for vf in video_files if vf.get("width", 0) >= 1920]
            if hd_files:
                best_video = max(hd_files, key=lambda x: x.get("width", 0))
            else:
                best_video = max(video_files, key=lambda x: x.get("width", 0))

            clip_url = best_video.get("link")
            if not clip_url:
                continue

            r = requests.get(clip_url, stream=True, timeout=120)
            r.raise_for_status()
            tmp_clip = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp_clip.write(chunk)
            tmp_clip.close()
            clip_path = tmp_clip.name

            probe_clip = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", clip_path],
                stdout=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            try:
                clip_duration = float(probe_clip.stdout.strip())
            except Exception:
                clip_duration = 5.0

            scene_paths.append((clip_path, clip_duration))
            total_scene_duration += clip_duration

            if total_scene_duration >= real_duration * 1.2:
                break

        if not scene_paths:
            return jsonify({
                "success": False,
                "error": "Nessuna clip valida ottenuta da Pexels",
                "video_url": None,
                "duration": None,
            }), 500

        # 5. Normalizza e concatena
        normalized_clips = []
        for i, (clip_path, _dur) in enumerate(scene_paths):
            normalized_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            normalized_path = normalized_tmp.name
            normalized_tmp.close()

            normalize_cmd = [
                "ffmpeg", "-y",
                "-i", clip_path,
                "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-an",
                normalized_path,
            ]
            subprocess.run(normalize_cmd, timeout=120, check=True)
            normalized_clips.append(normalized_path)

        concat_list_tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
        for norm_path in normalized_clips:
            concat_list_tmp.write(f"file '{norm_path}'\n")
        concat_list_tmp.close()

        video_looped_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_looped_path = video_looped_tmp.name
        video_looped_tmp.close()

        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_tmp.name,
            "-c", "copy",
            "-t", str(real_duration),
            video_looped_path,
        ]
        subprocess.run(concat_cmd, timeout=300, check=True)
        os.unlink(concat_list_tmp.name)

        for norm_path in normalized_clips:
            try:
                if os.path.exists(norm_path):
                    os.unlink(norm_path)
            except Exception:
                pass

        # 6. final merge
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

        # 7. upload su R2
        s3_client = get_s3_client()

        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        object_key = f"videos/{today}/{uuid.uuid4().hex}.mp4"

        s3_client.upload_file(
            Filename=final_video_path,
            Bucket=R2_BUCKET_NAME,
            Key=object_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )

        public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"

        # 8. ROTAZIONE R2 - Cancella tutti i video vecchi tranne questo
        print("🔄 Avvio rotazione R2...")
        cleanup_old_videos(s3_client, object_key)

        # cleanup locali
        try:
            if audiopath and os.path.exists(audiopath):
                os.unlink(audiopath)
        except Exception:
            pass
        try:
            if audio_wav_path and os.path.exists(audio_wav_path):
                os.unlink(audio_wav_path)
        except Exception:
            pass
        try:
            if video_looped_path and os.path.exists(video_looped_path):
                os.unlink(video_looped_path)
        except Exception:
            pass
        try:
            if final_video_path and os.path.exists(final_video_path):
                os.unlink(final_video_path)
        except Exception:
            pass
        for clip_path, _dur in scene_paths:
            try:
                if clip_path and os.path.exists(clip_path):
                    os.unlink(clip_path)
            except Exception:
                pass

        return jsonify({
            "success": True,
            "error": None,
            "video_url": public_url,
            "duration": real_duration,
        })

    except Exception as e:
        try:
            if audiopath and os.path.exists(audiopath):
                os.unlink(audiopath)
        except Exception:
            pass
        try:
            if audio_wav_path and os.path.exists(audio_wav_path):
                os.unlink(audio_wav_path)
        except Exception:
            pass
        try:
            if video_looped_path and os.path.exists(video_looped_path):
                os.unlink(video_looped_path)
        except Exception:
            pass
        try:
            if final_video_path and os.path.exists(final_video_path):
                os.unlink(final_video_path)
        except Exception:
            pass
        for clip_path, _dur in scene_paths:
            try:
                if clip_path and os.path.exists(clip_path):
                    os.unlink(clip_path)
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

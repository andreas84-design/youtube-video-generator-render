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

# Pexels API
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")

def get_s3_client():
    """Client S3 configurato per Cloudflare R2"""
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
    """Cancella tutti i video MP4 in R2 TRANNE quello appena caricato"""
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix="videos/")
        
        deleted_count = 0
        for page in pages:
            if 'Contents' not in page:
                continue
                
            for obj in page['Contents']:
                key = obj['Key']
                if key.endswith('.mp4') and key != current_key:
                    s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                    deleted_count += 1
                    print(f"🗑️  Cancellato vecchio video: {key}", flush=True)
        
        if deleted_count > 0:
            print(f"✅ Rotazione completata: {deleted_count} video vecchi rimossi", flush=True)
        else:
            print("✅ Nessun video vecchio da rimuovere", flush=True)
            
    except Exception as e:
        print(f"⚠️  Errore rotazione R2 (video vecchi restano): {str(e)}", flush=True)

def translate_broll_keywords(keywords_text, script_context=""):
    """Traduce keywords italiane → inglese + contesto script"""
    if not keywords_text:
        return "women health wellness sleep"
    
    try:
        parts = [p.strip() for p in keywords_text.split(",") if p.strip()]
        translated = []
        for part in parts:
            result = GoogleTranslator(source='it', target='en').translate(part)
            translated.append(result.lower())
        
        base_query = " ".join(translated)
        if script_context:
            full_query = f"{base_query} {script_context[:20]}"
        else:
            full_query = base_query
            
        print(f"🌐 Traduzione: '{keywords_text}' + '{script_context[:30]}' → '{full_query[:60]}'", flush=True)
        return full_query[:100]
        
    except Exception as e:
        print(f"⚠️ Errore traduzione: {e}", flush=True)
        fallback_map = {
            "donne": "women", "menopausa": "menopause", "vampate": "hot flashes",
            "vampate di calore": "hot flashes", "sonno": "sleep", "insonnia": "insomnia",
            "dimagrimento": "weight loss", "pancia gonfia": "bloating",
            "articolazioni": "joints", "dolore": "pain", "ginocchia": "knees",
            "benessere": "wellness", "salute": "health",
        }
        parts = [p.strip().lower() for p in keywords_text.split(",")]
        translated = [fallback_map.get(p, p) for p in parts]
        return " ".join(translated)[:100]

@app.route("/ffmpeg-test", methods=["GET"])
def ffmpeg_test():
    """Test FFmpeg"""
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
    """🎬 SCENE-SYNC + R2"""
    audiopath = None
    audio_wav_path = None
    video_looped_path = None
    final_video_path = None
    scene_paths = []

    try:
        # Controllo config
        if not all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL, PEXELS_API_KEY]):
            return jsonify({
                "success": False,
                "error": "Config mancante (R2 o PEXELS_API_KEY)",
                "video_url": None,
                "duration": None,
            }), 500

        data = request.get_json(force=True) or {}
        audiobase64 = data.get("audiobase64")

        # --- SCRIPT (lista o stringa) ---
        raw_script = data.get("script", "")
        if isinstance(raw_script, list):
            script = " ".join(str(p).strip() for p in raw_script)
        else:
            script = str(raw_script).strip()

        # --- KEYWORDS (lista o stringa) ---
        raw_keywords = data.get("keywords", "")
        if isinstance(raw_keywords, list):
            sheet_keywords = ", ".join(str(k).strip() for k in raw_keywords)
        else:
            sheet_keywords = str(raw_keywords).strip()

        print("="*80, flush=True)
        print(f"🎬 START: {len(script)} char script, keywords: '{sheet_keywords}'", flush=True)

        if not audiobase64:
            return jsonify({"success": False, "error": "audiobase64 mancante"}), 400

        # 1. Audio processing
        audio_bytes = base64.b64decode(audiobase64)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(audio_bytes)
            audiopath = f.name

        audio_wav_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        audio_wav_path = audio_wav_tmp.name
        audio_wav_tmp.close()

        convert_audio_cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", audiopath,
            "-acodec", "pcm_s16le", "-ar", "48000", audio_wav_path,
        ]
        subprocess.run(convert_audio_cmd, timeout=60, check=True)
        os.unlink(audiopath)
        audiopath = audio_wav_path

        # 2. Real duration
        ffprobe_cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audiopath,
        ]
        probe = subprocess.run(ffprobe_cmd, stdout=subprocess.PIPE, text=True, timeout=10)
        real_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 720.0

        print(f"⏱️  Durata audio: {real_duration/60:.1f}min ({real_duration:.0f}s)", flush=True)

        # 🔥 STEP 1: SCENE-SYNC
        script_words = script.lower().split()
        words_per_second = len(script_words) / real_duration if real_duration > 0 else 2.5        
        print(f"🎯 SYNC: {len(script_words)} parole / {real_duration:.0f}s = {words_per_second:.1f} w/s", flush=True)

        # Genera 25 scene
        scene_assignments = []
        avg_scene_duration = real_duration / 25
        
        for i in range(25):
            timestamp = i * avg_scene_duration
            word_index = int(timestamp * words_per_second)
            scene_context = " ".join(script_words[word_index:word_index+5]) if word_index < len(script_words) else "wellness"
            scene_query = translate_broll_keywords(sheet_keywords, scene_context)
            
            scene_assignments.append({
                'scene': i+1, 'timestamp': round(timestamp, 1),
                'context': scene_context[:30], 'query': scene_query[:50]
            })

        # 🔥 STEP 2: DOWNLOAD PEXELS
for assignment in scene_assignments:
    print(f"📍 Scene {assignment['scene']}: {assignment['timestamp']}s → '{assignment['context']}'", flush=True)
    
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": assignment['query'], "orientation": "landscape", "per_page": 1}
    
    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params=params,
            timeout=15
        )
        if resp.status_code == 200 and resp.json().get('videos'):
            video_url = resp.json()['videos'][0]['video_files'][0]['link']

            tmp_clip = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            clip_resp = requests.get(video_url, stream=True, timeout=30)
            clip_resp.raise_for_status()
            for chunk in clip_resp.iter_content(chunk_size=1024*1024):
                if chunk:
                    tmp_clip.write(chunk)
            tmp_clip.close()

            scene_paths.append((tmp_clip.name, min(4.0, avg_scene_duration)))

    except Exception as e:
        print(f"⚠️ Errore download clip Pexels scena {assignment['scene']}: {e}", flush=True)
        continue

        print(f"✅ CLIPS SCARICATE: {len(scene_paths)}/25", flush=True)

        # 3. Normalize clips
        normalized_clips = []
        for i, (clip_path, _dur) in enumerate(scene_paths):
            normalized_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            normalized_path = normalized_tmp.name
            normalized_tmp.close()

            normalize_cmd = [
                "ffmpeg", "-y", "-loglevel", "error", "-i", clip_path,
                "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an", normalized_path,
            ]
            subprocess.run(normalize_cmd, timeout=120, check=True)
            normalized_clips.append(normalized_path)

        print(f"🎞️ NORMALIZED CLIPS: {len(normalized_clips)}", flush=True)
        for p in normalized_clips:
            print(f"   - {p}", flush=True)

        # Se per qualche motivo c'è solo una clip, la usiamo in loop
        if not normalized_clips:
            raise RuntimeError("Nessuna clip normalizzata disponibile")
        
        # 4. Concat tutte le clip
        concat_list_tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
        for norm_path in normalized_clips:
            concat_list_tmp.write(f"file '{norm_path}'\n")
        concat_list_tmp.close()

        video_looped_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_looped_path = video_looped_tmp.name
        video_looped_tmp.close()

        concat_cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_list_tmp.name,
            "-vf", "fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-t", str(real_duration),
            video_looped_path,
        ]
        subprocess.run(concat_cmd, timeout=300, check=True)
        os.unlink(concat_list_tmp.name)

        # 5. Merge video + audio
        final_video_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        final_video_path = final_video_tmp.name
        final_video_tmp.close()

        merge_cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_looped_path, "-i", audiopath,
            "-filter_complex",
            "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            "fade=t=in:st=0:d=0.3,fade=t=out:st=2:d=0.3[fg];"
            "[fg]format=yuv420p[outv]",
            "-map", "[outv]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            final_video_path,
        ]
        subprocess.run(merge_cmd, timeout=300, check=True)

        # 6. Upload R2 + rotazione
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
        cleanup_old_videos(s3_client, object_key)

        # Cleanup
        for path in [audiopath, video_looped_path, final_video_path] + normalized_clips + [p[0] for p in scene_paths]:
            try:
                os.unlink(path)
            except:
                pass

        print(f"✅ VIDEO COMPLETO: {real_duration/60:.1f}min → {public_url}", flush=True)
        
        return jsonify({
            "success": True,
            "clips_used": len(scene_paths),
            "duration": real_duration,
            "words_per_second": words_per_second,
            "video_url": public_url,
            "scenes": scene_assignments[:3],
        })

    except Exception as e:
        print(f"❌ ERRORE: {e}", flush=True)
        # Cleanup error
        for path in [audiopath, audio_wav_path, video_looped_path, final_video_path] + [p[0] for p in scene_paths]:
            try:
                os.unlink(path)
            except:
                pass
        return jsonify({"success": False, "error": str(e), "video_url": None}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

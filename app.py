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
import math
import random

app = Flask(__name__)

# Config R2 (S3 compatibile)
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL")
R2_REGION = os.environ.get("R2_REGION", "auto")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")

# Pexels / Pixabay API
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")

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
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix="videos/")

        deleted_count = 0
        for page in pages:
            if "Contents" not in page:
                continue

            for obj in page["Contents"]:
                key = obj["Key"]
                if key.endswith(".mp4") and key != current_key:
                    s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                    deleted_count += 1
                    print(f"🗑️  Cancellato vecchio video: {key}", flush=True)

        if deleted_count > 0:
            print(f"✅ Rotazione completata: {deleted_count} video vecchi rimossi", flush=True)
        else:
            print("✅ Nessun video vecchio da rimuovere", flush=True)

    except Exception as e:
        print(f"⚠️  Errore rotazione R2 (video vecchi restano): {str(e)}", flush=True)

# -------------------------------------------------
# Mapping SCENA → QUERY visiva (canale dimagrimento 40+)
# -------------------------------------------------
def pick_visual_query(context: str, keywords_text: str = "") -> str:
    """
    Converte il contesto della scena (in italiano) in una query corta e visiva.
    Pensato per nicchia: salute/dimagrimento donne 40+.
    """
    ctx = (context or "").lower()

    def q(action: str, setting: str = "", mood: str = "", extra: str = "") -> str:
        base = "woman 45"
        parts = [base, action, setting, mood, extra]
        return " ".join([p for p in parts if p]).strip()

    # Routine mattutina
    if any(w in ctx for w in ["mattina", "risveglio", "appena sveglia", "iniziare la giornata"]):
        return q("morning routine", "bathroom mirror", "natural light")

    # Routine serale / sonno
    if any(w in ctx for w in ["prima di dormire", "sera tardi", "routine serale", "sonno profondo"]):
        return q("night routine", "bedroom", "warm light")

    # Pancia gonfia / digestione
    if any(w in ctx for w in ["pancia gonfia", "gonfiore", "gonfia", "digestione", "digerire", "intestino", "colon"]):
        return "woman 45 bloated belly closeup bathroom"

    # Bilancia / peso / kg
    if any(w in ctx for w in ["bilancia", "pesarsi", "peso", "kg", "chili", "chilo"]):
        return "woman 45 stepping on scale bathroom"

    # Alimentazione sana / piatto
    if any(w in ctx for w in ["alimentazione", "dieta", "pasto", "colazione", "pranzo", "cena", "verdure", "insalata", "frutta", "piatto sano", "porzioni"]):
        return q("preparing healthy meal", "kitchen", "focused", "colorful vegetables")

    # Bere acqua / idratazione
    if any(w in ctx for w in ["bere acqua", "idrat", "bicchiere d'acqua", "bottiglia d'acqua"]):
        return q("drinking water", "kitchen", "daylight")

    # Allenamento / yoga
    if any(w in ctx for w in ["allenamento", "yoga", "stretching", "esercizio", "workout"]):
        return q("home workout", "living room", "energetic")

    # Liste / step / consigli
    if any(w in ctx for w in ["passo dopo passo", "step", "passaggi", "consigli", "strategie", "ecco cosa fare"]):
        return "checklist animation health tips woman"

    # fallback wellness generico
    return "woman 45 wellness lifestyle kitchen home"

# 🚀 NUOVA FUNZIONE CON FILTRO ANTI-NATURA
def fetch_clip_for_scene(scene_number: int, query: str, avg_scene_duration: float):
    """🚫 ELIMINA NATURA/UOMINI/ANIMALI - FORZA DONNE 40+ WELLNESS"""
    target_duration = min(4.0, avg_scene_duration)

    def is_women_wellness_video_metadata(video_data, source):
        """Filtro METADATA: solo donne wellness, NO natura/uomini/animali"""
        # OBBLIGATORIO: almeno 1 keyword DONNA
        required = ['woman', 'female', 'girl', 'lady', 'women']
        # 🚫 VIETATO: natura/uomini/animali
        banned = [
            'man', 'men', 'male', 'boy', 'guy',
            'dog', 'cat', 'animal', 'pet', 'wildlife',
            'nature', 'landscape', 'mountain', 'forest', 'beach', 
            'tree', 'sky', 'ocean', 'waterfall', 'field', 'grass',
            'bird', 'fish', 'horse', 'sunset', 'drone'
        ]
        
        if source == 'pexels':
            text = (video_data.get('description', '') + ' ' + 
                   ' '.join(video_data.get('tags', []))).lower()
        else:  # pixabay
            text = ' '.join(video_data.get('tags', [])).lower()
        
        has_woman = any(kw in text for kw in required)
        has_banned = any(kw in text for kw in banned)
        
        print(f"🔍 [{source}] '{text[:60]}...' → woman:{has_woman} banned:{has_banned}", flush=True)
        return has_woman and not has_banned

    def download_file(url: str) -> str:
        tmp_clip = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        clip_resp = requests.get(url, stream=True, timeout=30)
        clip_resp.raise_for_status()
        for chunk in clip_resp.iter_content(chunk_size=1024 * 1024):
            if chunk: tmp_clip.write(chunk)
        tmp_clip.close()
        return tmp_clip.name

    # --- PEXELS CON FILTRO ---
    def try_pexels():
        if not PEXELS_API_KEY: return None
        headers = {"Authorization": PEXELS_API_KEY}
        # FORZA DONNE + ESCLUDE NATURA
        params = {
            "query": f"{query} woman over 40 female kitchen home -nature -man -animal -landscape",
            "orientation": "landscape",
            "per_page": 20,
            "page": random.randint(1, 5),
        }
        resp = requests.get("https://api.pexels.com/videos/search", 
                          headers=headers, params=params, timeout=20)
        if resp.status_code != 200: return None
        
        videos = resp.json().get("videos", [])
        women_videos = [v for v in videos if is_women_wellness_video_metadata(v, 'pexels')]
        
        print(f"🔍 Pexels: {len(videos)} totali → {len(women_videos)} DONNE OK", flush=True)
        if women_videos:
            video = random.choice(women_videos)
            for vf in video.get("video_files", []):
                if vf.get("width", 0) >= 1280:
                    return download_file(vf["link"])
        return None

    # --- PIXABAY CON FILTRO ---
    def try_pixabay():
        if not PIXABAY_API_KEY: return None
        params = {
            "key": PIXABAY_API_KEY,
            "q": f"{query} woman female kitchen home -nature -man -animal",
            "per_page": 20,
            "safesearch": "true",
            "min_width": 1280,
        }
        resp = requests.get("https://pixabay.com/api/videos/", params=params, timeout=20)
        if resp.status_code != 200: return None
        
        hits = resp.json().get("hits", [])
        for hit in hits:
            if is_women_wellness_video_metadata(hit, 'pixabay'):
                videos = hit.get("videos", {})
                for quality in ["large", "medium", "small"]:
                    if quality in videos and "url" in videos[quality]:
                        return download_file(videos[quality]["url"])
        return None

    # Pexels → Pixabay
    for source_name, func in [("Pexels", try_pexels), ("Pixabay", try_pixabay)]:
        try:
            path = func()
            if path:
                print(f"🎥 Scena {scene_number}: '{query}' → {source_name} (DONNA VERIFICATA)", flush=True)
                return path, target_duration
            print(f"⚠️ {source_name}: nessuna clip DONNA trovata", flush=True)
        except Exception as e:
            print(f"❌ {source_name}: {e}", flush=True)
    
    print(f"❌ NO CLIP DONNA per scena {scene_number}: '{query}'", flush=True)
    return None, None

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
        if not all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL]):
            return (jsonify({"success": False, "error": "Config R2 mancante", "video_url": None, "duration": None}), 500)

        data = request.get_json(force=True) or {}
        audiobase64 = data.get("audio_base64") or data.get("audiobase64")

        raw_script = (data.get("script") or data.get("script_chunk") or data.get("script_audio") or data.get("script_completo") or "")
        if isinstance(raw_script, list):
            script = " ".join(str(p).strip() for p in raw_script)
        else:
            script = str(raw_script).strip()

        raw_keywords = data.get("keywords", "")
        if isinstance(raw_keywords, list):
            sheet_keywords = ", ".join(str(k).strip() for k in raw_keywords)
        else:
            sheet_keywords = str(raw_keywords).strip()

        print("=" * 80, flush=True)
        print(f"🎬 START: {len(script)} char script, keywords: '{sheet_keywords}'", flush=True)

        if not audiobase64:
            return jsonify({"success": False, "error": "audiobase64 mancante"}), 400

        # 1. Audio processing
        audio_bytes = base64.b64decode(audiobase64)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(audio_bytes)
            audiopath_tmp = f.name

        audio_wav_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        audio_wav_path = audio_wav_tmp.name
        audio_wav_tmp.close()

        convert_audio_cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", audiopath_tmp, "-acodec", "pcm_s16le", "-ar", "48000", audio_wav_path]
        subprocess.run(convert_audio_cmd, timeout=60, check=True)
        os.unlink(audiopath_tmp)
        audiopath = audio_wav_path

        # 2. Real duration
        ffprobe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audiopath]
        probe = subprocess.run(ffprobe_cmd, stdout=subprocess.PIPE, text=True, timeout=10)
        real_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 720.0

        print(f"⏱️  Durata audio: {real_duration/60:.1f}min ({real_duration:.0f}s)", flush=True)

        # SCENE-SYNC
        script_words = script.lower().split()
        words_per_second = len(script_words) / real_duration if real_duration > 0 else 2.5
        avg_scene_duration = real_duration / 25

        scene_assignments = []
        for i in range(25):
            timestamp = i * avg_scene_duration
            word_index = int(timestamp * words_per_second)
            if word_index < len(script_words):
                scene_context = " ".join(script_words[word_index: word_index + 7])
            else:
                scene_context = "wellness donna 45 salute dimagrimento"

            scene_query = pick_visual_query(scene_context, sheet_keywords)
            scene_assignments.append({"scene": i + 1, "timestamp": round(timestamp, 1), "context": scene_context[:60], "query": scene_query[:80]})

        # DOWNLOAD CLIP CON FILTRO
        for assignment in scene_assignments:
            print(f"📍 Scene {assignment['scene']}: {assignment['timestamp']}s → '{assignment['context']}'", flush=True)
            clip_path, clip_dur = fetch_clip_for_scene(assignment["scene"], assignment["query"], avg_scene_duration)
            if clip_path and clip_dur:
                scene_paths.append((clip_path, clip_dur))

        print(f"✅ CLIPS SCARICATE: {len(scene_paths)}/25", flush=True)

        # Normalize clips
        normalized_clips = []
        for i, (clip_path, _dur) in enumerate(scene_paths):
            try:
                print(f"🔧 Normalizing clip {i+1}/{len(scene_paths)}: {clip_path}", flush=True)
                normalized_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                normalized_path = normalized_tmp.name
                normalized_tmp.close()

                normalize_cmd = [
                    "ffmpeg", "-y", "-loglevel", "error", "-i", clip_path,
                    "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-an", normalized_path
                ]
                subprocess.run(normalize_cmd, timeout=120, check=True)

                if os.path.exists(normalized_path) and os.path.getsize(normalized_path) > 1000:
                    normalized_clips.append(normalized_path)
                    print(f"✅ Clip {i+1} normalizzata: {normalized_path}", flush=True)
                else:
                    print(f"⚠️ Clip {i+1} normalizzata ma file vuoto, SKIP", flush=True)
                    if os.path.exists(normalized_path):
                        os.unlink(normalized_path)

            except Exception as e:
                print(f"⚠️ Clip {i+1} ERRORE: {str(e)}, SKIP", flush=True)
                if 'normalized_path' in locals() and os.path.exists(normalized_path):
                    os.unlink(normalized_path)

        print(f"🎞️ NORMALIZED CLIPS: {len(normalized_clips)}/{len(scene_paths)}", flush=True)

        if not normalized_clips:
            raise RuntimeError("Nessuna clip normalizzata disponibile")

        # Concat clips
        total_clips_duration = 0.0
        for norm_path in normalized_clips:
            probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", norm_path]
            result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, text=True, timeout=10)
            dur_str = result.stdout.strip()
            clip_dur = float(dur_str) if dur_str else 4.0
            total_clips_duration += clip_dur

        print(f"🎞️ Durata clip totali: {total_clips_duration:.1f}s vs audio: {real_duration:.1f}s", flush=True)

        MAX_CONCAT_ENTRIES = 150
        concat_list_tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
        entries_written = 0

        if total_clips_duration < real_duration and len(normalized_clips) > 1:
            loops_needed = math.ceil(real_duration / total_clips_duration)
            print(f"🔁 Loop sequenza clip {loops_needed}x per coprire audio", flush=True)
            for _ in range(loops_needed):
                for norm_path in normalized_clips:
                    if entries_written >= MAX_CONCAT_ENTRIES: break
                    concat_list_tmp.write(f"file '{norm_path}'\n")
                    entries_written += 1
                if entries_written >= MAX_CONCAT_ENTRIES: break
        else:
            for norm_path in normalized_clips:
                concat_list_tmp.write(f"file '{norm_path}'\n")
                entries_written += 1

        concat_list_tmp.close()
        print(f"📝 File concat con {entries_written} entries", flush=True)

        video_looped_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_looped_path = video_looped_tmp.name
        video_looped_tmp.close()

        concat_cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat_list_tmp.name,
            "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-t", str(real_duration), video_looped_path
        ]
        subprocess.run(concat_cmd, timeout=600, check=True)
        os.unlink(concat_list_tmp.name)

        # Merge video + audio
        final_video_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        final_video_path = final_video_tmp.name
        final_video_tmp.close()

        merge_cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", video_looped_path, "-i", audiopath,
            "-filter_complex", "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p[v]",
            "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-shortest", final_video_path
        ]
        subprocess.run(merge_cmd, timeout=300, check=True)

        # Upload R2 + rotazione
        s3_client = get_s3_client()
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        object_key = f"videos/{today}/{uuid.uuid4().hex}.mp4"

        s3_client.upload_file(Filename=final_video_path, Bucket=R2_BUCKET_NAME, Key=object_key, ExtraArgs={"ContentType": "video/mp4"})
        public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"
        cleanup_old_videos(s3_client, object_key)

        # Cleanup
        for path in [audiopath, video_looped_path, final_video_path] + normalized_clips + [p[0] for p in scene_paths]:
            try: os.unlink(path)
            except: pass

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
        for path in [audiopath, audio_wav_path, video_looped_path, final_video_path] + [p[0] for p in scene_paths]:
            try: os.unlink(path)
            except: pass
        return jsonify({"success": False, "error": str(e), "video_url": None}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

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
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")  # <--- nuova env var


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

    # Sonno / stress / relax
    if any(
        w in ctx
        for w in [
            "sonno",
            "dorm",
            "insonnia",
            "addormentar",
            "rilassamento",
            "stress",
            "ansia",
            "riposo",
            "notte",
        ]
    ):
        return "woman 45 sleeping bedroom"

    # Alimentazione / cucina sana / pancia gonfia
    if any(
        w in ctx
        for w in [
            "alimentazione",
            "dieta",
            "cibo",
            "pasto",
            "colazione",
            "pranzo",
            "cena",
            "verdure",
            "frutta",
            "pancia gonfia",
            "gonfiore",
            "mangiare",
        ]
    ):
        return "woman 45 healthy food kitchen"

    # Attività fisica / movimento / metabolismo attivo
    if any(
        w in ctx
        for w in [
            "camminata",
            "passeggiata",
            "camminare",
            "allenamento",
            "attività fisica",
            "esercizio",
            "metabolismo",
            "muoversi",
            "sport",
            "yoga",
            "workout",
        ]
    ):
        return "woman 45 walking outdoor"

    # Energia / stanchezza / burnout
    if any(
        w in ctx
        for w in [
            "energia",
            "stanchezza",
            "stanca",
            "affaticamento",
            "spossatezza",
            "fiato corto",
            "spenta",
        ]
    ):
        return "tired woman 45 then energetic woman 45 home"

    # Ormoni / menopausa / vampate / medico
    if any(
        w in ctx
        for w in [
            "ormoni",
            "ormonale",
            "menopausa",
            "perimenopausa",
            "vampata",
            "vampate",
            "sudorazione",
            "medico",
            "visita",
            "analisi",
            "ginecologo",
        ]
    ):
        return "woman 45 doctor consultation"

    # Mindset / motivazione / autostima
    if any(
        w in ctx
        for w in [
            "motivazione",
            "autostima",
            "mentalità",
            "mindset",
            "obiettivi",
            "cambiare vita",
            "costanza",
            "disciplina",
        ]
    ):
        return "confident woman 45 smiling outdoor"

    # Se ho keywords generiche dal foglio, provo traduzione
    try:
        if keywords_text:
            first_kw = keywords_text.split(",")[0].strip()
            if first_kw:
                kw_en = GoogleTranslator(source="it", target="en").translate(first_kw[:40])
                return kw_en.lower()
    except Exception as e:
        print(f"⚠️ Errore traduzione keyword singola: {e}", flush=True)

    # Fallback wellness generico
    return "woman 45 wellness lifestyle"


def fetch_clip_for_scene(scene_number: int, query: str, avg_scene_duration: float):
    """
    Alterna automaticamente:
    - scene pari  → Pexels
    - scene dispari → Pixabay
    con fallback incrociato se una delle due non trova risultati.
    """
    # Durata target max 4s per clip
    target_duration = min(4.0, avg_scene_duration)

    def download_file(url: str) -> str:
        tmp_clip = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        clip_resp = requests.get(url, stream=True, timeout=30)
        clip_resp.raise_for_status()
        for chunk in clip_resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                tmp_clip.write(chunk)
        tmp_clip.close()
        return tmp_clip.name

    # --- PEXELS ---
    def try_pexels():
        if not PEXELS_API_KEY:
            return None
        headers = {"Authorization": PEXELS_API_KEY}
        params = {
            "query": query,
            "orientation": "landscape",
            "per_page": 3,
            "page": random.randint(1, 3),
        }
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params=params,
            timeout=15,
        )
        if resp.status_code == 200 and resp.json().get("videos"):
            videos = resp.json()["videos"]
            video = random.choice(videos)
            # Prendo il primo file MP4 landscape
            file_link = None
            for vf in video.get("video_files", []):
                if vf.get("width", 0) >= 960:
                    file_link = vf.get("link")
                    break
            if not file_link and video.get("video_files"):
                file_link = video["video_files"][0]["link"]
            if file_link:
                return download_file(file_link)
        return None

    # --- PIXABAY ---
    def try_pixabay():
        if not PIXABAY_API_KEY:
            return None
        params = {
            "key": PIXABAY_API_KEY,
            "q": query,
            "per_page": random.randint(3, 10),
            "page": random.randint(1, 5),
        }
        resp = requests.get(
            "https://pixabay.com/api/videos/",
            params=params,
            timeout=15,
        )
        data = resp.json() if resp.status_code == 200 else {}
        hits = data.get("hits") or []
        if hits:
            video = random.choice(hits)
            # uso small/tiny per velocità
            videos = video.get("videos", {})
            for quality in ["small", "tiny", "medium", "large"]:
                if quality in videos and "url" in videos[quality]:
                    return download_file(videos[quality]["url"])
        return None

    # ordine: pari Pexels→Pixabay, dispari Pixabay→Pexels
    first, second = (try_pexels, try_pixabay) if scene_number % 2 == 0 else (try_pixabay, try_pexels)

    for func in (first, second):
        try:
            path = func()
            if path:
                print(
                    f"🎥 Scena {scene_number}: query '{query}' → clip da {'Pexels' if func is try_pexels else 'Pixabay'}",
                    flush=True,
                )
                return path, target_duration
        except Exception as e:
            print(
                f"⚠️ Errore download scena {scene_number} ({'Pexels' if func is try_pexels else 'Pixabay'}): {e}",
                flush=True,
            )
            continue

    print(f"⚠️ Nessuna clip per scena {scene_number} (query='{query}')", flush=True)
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
        # Controllo config
        if not all(
            [R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL]
        ):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Config R2 mancante",
                        "video_url": None,
                        "duration": None,
                    }
                ),
                500,
            )

        data = request.get_json(force=True) or {}
        audiobase64 = data.get("audio_base64") or data.get("audiobase64")

        # --- SCRIPT (lista o stringa) ---
        raw_script = (
            data.get("script")
            or data.get("script_chunk")
            or data.get("script_audio")
            or data.get("script_completo")
            or ""
        )
        if isinstance(raw_script, list):
            script = " ".join(str(p).strip() for p in raw_script)
        else:
            script = str(raw_script).strip()

        # --- DEBUG ---
        print(f"🔍 DEBUG: keys ricevute = {list(data.keys())}", flush=True)
        print(f"🔍 DEBUG: raw_script type = {type(raw_script)}, len = {len(str(raw_script))}", flush=True)
        print(f"🔍 DEBUG: script finale len = {len(script)}", flush=True)
        print(f"🔍 DEBUG: script prime 100 char = {script[:100]}", flush=True)

        # --- KEYWORDS (lista o stringa) ---
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

        convert_audio_cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            audiopath_tmp,
            "-acodec",
            "pcm_s16le",
            "-ar",
            "48000",
            audio_wav_path,
        ]
        subprocess.run(convert_audio_cmd, timeout=60, check=True)
        os.unlink(audiopath_tmp)
        audiopath = audio_wav_path

        # 2. Real duration
        ffprobe_cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audiopath,
        ]
        probe = subprocess.run(ffprobe_cmd, stdout=subprocess.PIPE, text=True, timeout=10)
        real_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 720.0

        print(f"⏱️  Durata audio: {real_duration/60:.1f}min ({real_duration:.0f}s)", flush=True)

        # 🔥 STEP 1: SCENE-SYNC
        script_words = script.lower().split()
        words_per_second = len(script_words) / real_duration if real_duration > 0 else 2.5
        print(
            f"🎯 SYNC: {len(script_words)} parole / {real_duration:.0f}s = {words_per_second:.1f} w/s",
            flush=True,
        )

        # Genera 25 scene
        scene_assignments = []
        avg_scene_duration = real_duration / 25

        for i in range(25):
            timestamp = i * avg_scene_duration
            word_index = int(timestamp * words_per_second)
            if word_index < len(script_words):
                scene_context = " ".join(script_words[word_index: word_index + 7])
            else:
                scene_context = "wellness donna 45 salute dimagrimento"

            scene_query = pick_visual_query(scene_context, sheet_keywords)

            print(
                f"🌐 Query visiva: ctx='{scene_context[:40]}' → '{scene_query}'",
                flush=True,
            )

            scene_assignments.append(
                {
                    "scene": i + 1,
                    "timestamp": round(timestamp, 1),
                    "context": scene_context[:60],
                    "query": scene_query[:80],
                }
            )

        # 🔥 STEP 2: DOWNLOAD CLIP (Pexels + Pixabay alternati)
        for assignment in scene_assignments:
            print(
                f"📍 Scene {assignment['scene']}: {assignment['timestamp']}s → '{assignment['context']}'",
                flush=True,
            )
            clip_path, clip_dur = fetch_clip_for_scene(
                assignment["scene"],
                assignment["query"],
                avg_scene_duration,
            )
            if clip_path and clip_dur:
                scene_paths.append((clip_path, clip_dur))

        print(f"✅ CLIPS SCARICATE: {len(scene_paths)}/25", flush=True)

        # 3. Normalize clips
        normalized_clips = []
        for i, (clip_path, _dur) in enumerate(scene_paths):
            try:
                print(f"🔧 Normalizing clip {i+1}/{len(scene_paths)}: {clip_path}", flush=True)

                normalized_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                normalized_path = normalized_tmp.name
                normalized_tmp.close()

                normalize_cmd = [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    clip_path,
                    "-vf",
                    "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "23",
                    "-an",
                    normalized_path,
                ]
                subprocess.run(normalize_cmd, timeout=120, check=True)

                if os.path.exists(normalized_path) and os.path.getsize(normalized_path) > 1000:
                    normalized_clips.append(normalized_path)
                    print(f"✅ Clip {i+1} normalizzata: {normalized_path}", flush=True)
                else:
                    print(f"⚠️ Clip {i+1} normalizzata ma file vuoto, SKIP", flush=True)
                    if os.path.exists(normalized_path):
                        os.unlink(normalized_path)

            except subprocess.TimeoutExpired:
                print(f"⚠️ Clip {i+1} TIMEOUT durante normalize, SKIP", flush=True)
                if os.path.exists(normalized_path):
                    os.unlink(normalized_path)
            except subprocess.CalledProcessError as e:
                print(f"⚠️ Clip {i+1} ERRORE FFmpeg: {e}", flush=True)
                if os.path.exists(normalized_path):
                    os.unlink(normalized_path)
            except Exception as e:
                print(f"⚠️ Clip {i+1} ERRORE generico: {str(e)}, SKIP", flush=True)

        print(f"🎞️ NORMALIZED CLIPS: {len(normalized_clips)}/{len(scene_paths)}", flush=True)
        for p in normalized_clips:
            print(f"   ✓ {p}", flush=True)

        if not normalized_clips:
            raise RuntimeError("Nessuna clip normalizzata disponibile")

        if len(normalized_clips) < 3:
            print(f"⚠️ Solo {len(normalized_clips)} clip normalizzate, ma procedo...", flush=True)

        # 4. Concat tutte le clip, cercando di coprire l'audio senza loopare una sola clip
        total_clips_duration = 0.0
        for norm_path in normalized_clips:
            probe_cmd = [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                norm_path,
            ]
            result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, text=True, timeout=10)
            dur_str = result.stdout.strip()
            clip_dur = float(dur_str) if dur_str else 4.0
            total_clips_duration += clip_dur

        print(
            f"🎞️ Durata clip totali: {total_clips_duration:.1f}s vs audio: {real_duration:.1f}s",
            flush=True,
        )

        MAX_CONCAT_ENTRIES = 150

        concat_list_tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
        entries_written = 0

        if total_clips_duration < real_duration and len(normalized_clips) > 1:
            loops_needed = math.ceil(real_duration / total_clips_duration)
            print(f"🔁 Loop sequenza clip {loops_needed}x per coprire audio", flush=True)
            for _ in range(loops_needed):
                for norm_path in normalized_clips:
                    if entries_written >= MAX_CONCAT_ENTRIES:
                        break
                    concat_list_tmp.write(f"file '{norm_path}'\n")
                    entries_written += 1
                if entries_written >= MAX_CONCAT_ENTRIES:
                    break
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
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_tmp.name,
            "-vf",
            "fps=30,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-t",
            str(real_duration),
            video_looped_path,
        ]
        subprocess.run(concat_cmd, timeout=600, check=True)
        os.unlink(concat_list_tmp.name)

        # 5. Merge video + audio
        final_video_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        final_video_path = final_video_tmp.name
        final_video_tmp.close()

        merge_cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            video_looped_path,
            "-i",
            audiopath,
            "-filter_complex",
            "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,format=yuv420p[v]",
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
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
        for path in [audiopath, video_looped_path, final_video_path] + normalized_clips + [
            p[0] for p in scene_paths
        ]:
            try:
                os.unlink(path)
            except Exception:
                pass

        print(f"✅ VIDEO COMPLETO: {real_duration/60:.1f}min → {public_url}", flush=True)

        return jsonify(
            {
                "success": True,
                "clips_used": len(scene_paths),
                "duration": real_duration,
                "words_per_second": words_per_second,
                "video_url": public_url,
                "scenes": scene_assignments[:3],
            }
        )

    except Exception as e:
        print(f"❌ ERRORE: {e}", flush=True)
        for path in [
            audiopath,
            audio_wav_path,
            video_looped_path,
            final_video_path,
        ] + [p[0] for p in scene_paths]:
            try:
                os.unlink(path)
            except Exception:
                pass
        return jsonify({"success": False, "error": str(e), "video_url": None}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

import os
import base64
import subprocess
import tempfile
import json
import requests
from flask import Flask, request, jsonify
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips
from moviepy.config import change_settings
from pyPexels import PyPexels

change_settings({"FFMPEG_BINARY": "ffmpeg"})
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
        pexel = PyPexels(api_key=api_key)
        search = pexel.search_videos(query="meditation nature", orientation="landscape", size="medium", per_page=3)
        videos = search.get("videos", [])
        
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
        "audiobase64": "...",      # audio MP3 in base64 (Google TTS)
        "script": "...",           # testo script completo
        "audioduration": 180.0,    # durata stimata (fallback)
        "broll_keywords": "woman walking, healthy breakfast"  # keywords per Pexels
    }
    """
    audiopath = None
    pexels_clip_path = None
    final_video_path = None
    
    try:
        data = request.get_json(force=True) or {}
        audiobase64 = data.get("audiobase64")
        script = data.get("script", "")
        audioduration = data.get("audioduration")
        broll_keywords = data.get("broll_keywords", "").strip()
        
        # Fallback: usa prime parole script come topic se keywords mancano
        if not broll_keywords:
            words = script.split()[:8]
            broll_keywords = " ".join(words) if words else "wellness meditation"
        
        # Prendi prima keyword o usa tutto se corto
        query_keywords = broll_keywords.split(",")
        pexels_query = query_keywords[0].strip() if query_keywords else broll_keywords
        
        if not audiobase64:
            return jsonify({
                "success": False,
                "error": "audiobase64 mancante o vuoto",
                "videobase64": None,
                "duration": None,
            }), 400
        
        # 1. Decodifica base64 in MP3 temporaneo
        try:
            audio_bytes = base64.b64decode(audiobase64)
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Decodifica base64 fallita: {str(e)}",
                "videobase64": None,
                "duration": None,
            }), 400
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            f.write(audio_bytes)
            audiopath = f.name
        
        # 2. Leggi la durata reale dell'MP3 con ffprobe
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
        
        # Fallback se ffprobe fallisce
        if real_duration is None or real_duration <= 0:
            try:
                real_duration = float(audioduration)
            except (TypeError, ValueError):
                real_duration = 60.0
        
        # 3. Scarica clip Pexels
        api_key = os.environ.get("PEXELS_API_KEY")
        if not api_key:
            return jsonify({
                "success": False,
                "error": "PEXELS_API_KEY non configurata in Railway",
                "videobase64": None,
                "duration": None,
            }), 500
        
        pexel = PyPexels(api_key=api_key)
        search = pexel.search_videos(
            query=pexels_query,
            orientation="landscape",
            size="large",
            per_page=5
        )
        videos = search.get("videos", [])
        
        if not videos:
            return jsonify({
                "success": False,
                "error": f"Nessun video Pexels trovato per query: '{pexels_query}'",
                "videobase64": None,
                "duration": None,
            }), 500
        
        # Prendi il primo video e trova la versione HD
        video_files = videos[0].get("video_files", [])
        if not video_files:
            return jsonify({
                "success": False,
                "error": "Nessun file video disponibile nel risultato Pexels",
                "videobase64": None,
                "duration": None,
            }), 500
        
        # Scegli la versione con risoluzione >= 1920px (HD/FullHD)
        hd_files = [vf for vf in video_files if vf.get("width", 0) >= 1920]
        if hd_files:
            best_video = max(hd_files, key=lambda x: x.get("width", 0))
        else:
            # Fallback: prendi la risoluzione più alta disponibile
            best_video = max(video_files, key=lambda x: x.get("width", 0))
        
        video_url = best_video.get("link")
        if not video_url:
            return jsonify({
                "success": False,
                "error": "URL video Pexels non disponibile",
                "videobase64": None,
                "duration": None,
            }), 500
        
        # Scarica il video Pexels
        r = requests.get(video_url, stream=True, timeout=120)
        r.raise_for_status()
        
        pexels_clip_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        for chunk in r.iter_content(chunk_size=1024*1024):
            if chunk:
                pexels_clip_tmp.write(chunk)
        pexels_clip_tmp.close()
        pexels_clip_path = pexels_clip_tmp.name
        
        # 4. Elabora video con MoviePy
        video_clip = VideoFileClip(pexels_clip_path)
        
        # Resize a 1920x1080 mantenendo aspect ratio (crop se necessario)
        video_clip = video_clip.resize(height=1080)
        if video_clip.w < 1920:
            video_clip = video_clip.resize(width=1920)
        
        # Crop al centro se più largo/alto di 1920x1080
        if video_clip.w > 1920 or video_clip.h > 1080:
            video_clip = video_clip.crop(
                x_center=video_clip.w/2,
                y_center=video_clip.h/2,
                width=1920,
                height=1080
            )
        
        # Se la clip Pexels è più corta dell'audio, loopa
        if video_clip.duration < real_duration:
            loops_needed = int(real_duration / video_clip.duration) + 1
            video_clip = concatenate_videoclips([video_clip] * loops_needed)
        
        # Taglia alla durata esatta dell'audio
        video_clip = video_clip.subclip(0, min(video_clip.duration, real_duration))
        
        # 5. Aggiungi audio TTS
        audio_clip = AudioFileClip(audiopath)
        final_clip = video_clip.set_audio(audio_clip)
        
        # 6. Export video finale
        final_video_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        final_video_path = final_video_tmp.name
        
        final_clip.write_videofile(
            final_video_path,
            fps=25,
            codec="libx264",
            preset="medium",
            audio_codec="aac",
            audio_bitrate="192k",
            verbose=False,
            logger=None,
            threads=4
        )
        
        # Cleanup MoviePy (IMPORTANTE per evitare memory leak)
        final_clip.close()
        audio_clip.close()
        video_clip.close()
        
        # 7. Leggi video finale e converti in base64
        with open(final_video_path, "rb") as f:
            videobytes = f.read()
        videob64 = base64.b64encode(videobytes).decode("utf-8")
        
        # 8. Cleanup files temporanei
        for p in [audiopath, pexels_clip_path, final_video_path]:
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
        # Cleanup in caso di timeout
        for p in [audiopath, pexels_clip_path, final_video_path]:
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
        # Cleanup in caso di errore generico
        for p in [audiopath, pexels_clip_path, final_video_path]:
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

from flask import Flask, request, jsonify
import os
import requests
import tempfile
import base64

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        'service': 'YouTube Video Generator - Render.com',
        'status': 'running',
        'version': '2.0'
    })

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'message': 'Service is healthy'})

@app.route('/generate', methods=['POST'])
def generate():
    """Genera video con MoviePy (versione semplificata)"""
    try:
        from moviepy.editor import ColorClip, AudioFileClip, concatenate_videoclips
        
        data = request.json
        print(f"[API] Request received")
        
        # Validazione
        if not data or 'audio_url' not in data:
            return jsonify({'success': False, 'error': 'Missing audio_url'}), 400
        
        audio_url = data['audio_url']
        
        print(f"[API] Downloading audio: {audio_url}")
        
        # Download audio
        audio_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3').name
        response = requests.get(audio_url, timeout=60)
        response.raise_for_status()
        
        with open(audio_file, 'wb') as f:
            f.write(response.content)
        
        print(f"[API] Audio downloaded, creating video...")
        
        # Carica audio
        audio = AudioFileClip(audio_file)
        duration = audio.duration
        
        print(f"[API] Duration: {duration}s - Rendering background...")
        
        # Background rosa (colore femminile wellness)
        video = ColorClip(
            size=(1920, 1080), 
            color=(255, 229, 236),
            duration=duration
        )
        
        # Aggiungi audio
        final = video.set_audio(audio)
        
        # Output file
        output = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4').name
        
        print(f"[API] Rendering video (this takes 8-12 minutes)...")
        
        # Render con preset veloce
        final.write_videofile(
            output,
            fps=30,
            codec='libx264',
            audio_codec='aac',
            preset='ultrafast',
            threads=2,
            verbose=False,
            logger=None,
            audio_bitrate='128k',
            bitrate='2000k'
        )
        
        print(f"[API] Video rendered!")
        
        # Leggi come base64
        with open(output, 'rb') as f:
            video_bytes = f.read()
            video_base64 = base64.b64encode(video_bytes).decode('utf-8')
        
        file_size_mb = len(video_bytes) / 1024 / 1024
        
        # Cleanup
        try:
            os.remove(audio_file)
            os.remove(output)
        except:
            pass
        
        print(f"[API] Success! Size: {file_size_mb:.2f}MB")
        
        return jsonify({
            'success': True,
            'duration': duration,
            'video_base64': video_base64,
            'size_mb': round(file_size_mb, 2),
            'message': 'Video generated successfully'
        }), 200
        
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_trace = traceback.format_exc()
        
        print(f"[ERROR] {error_trace}")
        
        return jsonify({
            'success': False,
            'error': error_msg,
            'trace': error_trace
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"[START] Flask app on port {port}")
    app.run(host='0.0.0.0', port=port)

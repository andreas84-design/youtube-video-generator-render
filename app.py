from flask import Flask, request, jsonify
import os
import requests
from moviepy.editor import *
import tempfile

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        'service': 'YouTube Video Generator - Render.com',
        'status': 'running',
        'version': '1.0'
    })

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'message': 'Service is healthy'})

@app.route('/generate', methods=['POST'])
def generate():
    """Genera video completo con MoviePy"""
    try:
        data = request.json
        print(f"[API] Request received")
        
        # Validazione
        if not data or 'audio_url' not in data:
            return jsonify({'success': False, 'error': 'Missing audio_url'}), 400
        
        audio_url = data['audio_url']
        script = data.get('script', '')
        
        print(f"[API] Downloading audio from {audio_url}")
        
        # Download audio
        audio_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3').name
        response = requests.get(audio_url, timeout=60)
        response.raise_for_status()
        
        with open(audio_file, 'wb') as f:
            f.write(response.content)
        
        print(f"[API] Audio saved, loading clip...")
        
        # Carica audio
        audio = AudioFileClip(audio_file)
        duration = audio.duration
        
        print(f"[API] Audio duration: {duration}s - Creating video...")
        
        # Background video rosa (colore nicchia femminile)
        video = ColorClip(
            size=(1920, 1080), 
            color=(255, 229, 236),  # Rosa tenue
            duration=duration
        )
        
        # Aggiungi testo hook (primi 15s)
        if script:
            try:
                # Prendi prime 2 frasi per hook
                hook_text = '. '.join(script.split('.')[:2])[:80]
                
                txt_clip = TextClip(
                    hook_text,
                    fontsize=52,
                    color='white',
                    font='DejaVu-Sans-Bold',
                    stroke_color='deeppink',
                    stroke_width=3,
                    method='caption',
                    size=(1600, None),
                    align='center'
                )
                
                txt_clip = txt_clip.set_position(('center', 'center'))
                txt_clip = txt_clip.set_duration(min(15, duration))
                
                # Composite video + text
                video = CompositeVideoClip([video, txt_clip])
                
                print(f"[API] Text overlay added")
            except Exception as txt_err:
                print(f"[API] Text overlay failed (continuing): {txt_err}")
        
        # Aggiungi audio
        final = video.set_audio(audio)
        
        # Render con preset veloce
        output = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4').name
        
        print(f"[API] Rendering video to {output}...")
        
        final.write_videofile(
            output,
            fps=30,
            codec='libx264',
            audio_codec='aac',
            preset='ultrafast',  # Velocità massima
            threads=4,
            verbose=False,
            logger=None
        )
        
        print(f"[API] Video rendered successfully!")
        
        # Leggi video come base64 per ritornare
        import base64
        with open(output, 'rb') as f:
            video_bytes = f.read()
            video_base64 = base64.b64encode(video_bytes).decode('utf-8')
        
        # Cleanup
        try:
            os.remove(audio_file)
            os.remove(output)
        except:
            pass
        
        print(f"[API] Success! Video size: {len(video_bytes)/1024/1024:.2f}MB")
        
        return jsonify({
            'success': True,
            'duration': duration,
            'video_base64': video_base64,
            'size_mb': len(video_bytes)/1024/1024,
            'message': 'Video generated successfully'
        }), 200
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"[API ERROR] {error_trace}")
        
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"[API] Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port)

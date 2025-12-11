import os
import base64
import subprocess
import tempfile
import requests
from flask import Flask, request, jsonify
from moviepy.editor import ColorClip, AudioFileClip
from moviepy.config import change_settings

# Forza MoviePy a usare ffmpeg 7.0.2 di sistema
change_settings(FFMPEG_BINARY="ffmpeg")

app = Flask(__name__)

@app.route('/ffmpeg-test', methods=['GET'])
def ffmpeg_test():
    """Endpoint diagnostico per verificare versione ffmpeg"""
    result = subprocess.run(
        ['ffmpeg', '-version'], 
        stdout=subprocess.PIPE, 
        stderr=subprocess.STDOUT, 
        text=True
    )
    firstline = result.stdout.splitlines()[0] if result.stdout else "no output"
    return jsonify({'ffmpeg_output': firstline})


@app.route('/generate', methods=['POST'])
def generate():
    """
    Endpoint principale chiamato da n8n.
    
    Body JSON atteso:
    {
        "audiourl": "https://drive.google.com/uc?export=download&id=...",
        "script": "Testo script completo...",
        "audioduration": 90.5  # Durata in secondi (opzionale)
    }
    
    Risposta:
    {
        "success": true/false,
        "videobase64": "...",  # Video MP4 in base64
        "duration": 90.5,      # Durata effettiva audio
        "error": null          # Messaggio errore se success=false
    }
    """
    try:
        # 1. PARSING BODY JSON
        data = request.get_json(force=True) or {}
        audiourl = data.get('audiourl')
        script = data.get('script', '')
        audioduration = data.get('audioduration')
        
        # Validazione input
        if not audiourl:
            return jsonify({
                'success': False, 
                'error': 'audiourl mancante o vuoto',
                'videobase64': None,
                'duration': None
            }), 400
        
        # Converti audioduration in float (fallback 60s se mancante)
        try:
            audioduration = float(audioduration)
        except (TypeError, ValueError):
            audioduration = 60.0  # Fallback di sicurezza
        
        
        # 2. SCARICA MP3 DA AUDIOURL
        resp = requests.get(audiourl, timeout=120)
        if resp.status_code != 200:
            return jsonify({
                'success': False, 
                'error': f'Download audio fallito: status {resp.status_code}',
                'videobase64': None,
                'duration': None
            }), 400
        
        # Salva MP3 in file temporaneo
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as f:
            f.write(resp.content)
            audiopath = f.name
        
        
        # 3. CARICA AUDIO CON MOVIEPY E LEGGI DURATA REALE
        try:
            audioclip = AudioFileClip(audiopath)
            real_duration = audioclip.duration  # Durata effettiva del file MP3
        except Exception as e:
            os.unlink(audiopath)  # Pulisci file temp
            return jsonify({
                'success': False,
                'error': f'MoviePy non riesce a leggere audio: {str(e)}',
                'videobase64': None,
                'duration': None
            }), 400
        
        
        # 4. CREA VIDEO: SFONDO NERO + AUDIO SINCRONIZZATO
        # ColorClip nero 1920x1080 con durata = durata audio
        videoclip = ColorClip(size=(1920, 1080), color=(0, 0, 0))
        videoclip = videoclip.set_duration(real_duration)
        videoclip = videoclip.set_audio(audioclip)  # AGGIUNGI AUDIO AL VIDEO
        
        # Salva video MP4 in file temporaneo
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as vf:
            videopath = vf.name
            videoclip.write_videofile(
                videopath,
                fps=25,
                codec='libx264',
                audio_codec='aac',  # IMPORTANTE: esporta audio in AAC
                verbose=False,
                logger=None
            )
        
        
        # 5. LEGGI VIDEO MP4 E CONVERTI IN BASE64
        with open(videopath, 'rb') as f:
            videobytes = f.read()
        
        videob64 = base64.b64encode(videobytes).decode('utf-8')
        
        
        # 6. CLEANUP FILE TEMPORANEI
        audioclip.close()
        videoclip.close()
        os.unlink(audiopath)  # Elimina MP3 temp
        os.unlink(videopath)  # Elimina MP4 temp
        
        
        # 7. RISPOSTA SUCCESS
        return jsonify({
            'success': True,
            'error': None,
            'videobase64': videob64,
            'duration': real_duration
        }), 200
        
    except Exception as e:
        # Gestione errori imprevisti
        return jsonify({
            'success': False,
            'error': str(e),
            'videobase64': None,
            'duration': None
        }), 500


if __name__ == '__main__':
    # Solo per debug locale - Railway usa gunicorn
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=True)

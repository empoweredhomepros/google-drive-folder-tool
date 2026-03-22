from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import requests
import os
import tempfile
import glob
import time

app = Flask(__name__)

CORS(app, origins=[
    'https://google-drive-folder-tool.vercel.app',
    'http://localhost:3000',
    'http://localhost:5500',
])

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/download', methods=['POST'])
def download():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    url          = data.get('url', '').strip()
    access_token = data.get('accessToken', '').strip()
    folder_id    = data.get('folderId', '')
    custom_name  = data.get('filename', '').strip()

    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not access_token:
        return jsonify({'error': 'No access token provided'}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            ydl_opts = {
                'outtmpl':              os.path.join(tmpdir, '%(title)s.%(ext)s'),
                'format':               'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'merge_output_format':  'mp4',
                'noplaylist':           True,
                'quiet':                True,
                'no_warnings':          True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'video')

            # Find downloaded file
            files = glob.glob(os.path.join(tmpdir, '*'))
            if not files:
                return jsonify({'error': 'Download failed — no file produced'}), 500

            filepath  = files[0]
            file_size = os.path.getsize(filepath)

            # Use custom name if provided, otherwise use yt-dlp title
            if custom_name:
                file_name = custom_name if custom_name.lower().endswith('.mp4') else custom_name + '.mp4'
            else:
                file_name = os.path.basename(filepath)
                if not file_name.lower().endswith('.mp4'):
                    file_name = os.path.splitext(file_name)[0] + '.mp4'

            # --- Upload to Google Drive (resumable) ---
            metadata = {'name': file_name}
            if folder_id:
                metadata['parents'] = [folder_id]

            init_resp = requests.post(
                'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable',
                headers={
                    'Authorization':          f'Bearer {access_token}',
                    'Content-Type':           'application/json',
                    'X-Upload-Content-Type':  'video/mp4',
                    'X-Upload-Content-Length': str(file_size),
                },
                json=metadata,
            )

            if not init_resp.ok:
                return jsonify({'error': f'Drive init failed: {init_resp.text}'}), 500

            upload_url = init_resp.headers.get('Location')

            with open(filepath, 'rb') as f:
                up_resp = requests.put(
                    upload_url,
                    data=f,
                    headers={
                        'Content-Type':   'video/mp4',
                        'Content-Length': str(file_size),
                    },
                )

            if not up_resp.ok:
                return jsonify({'error': f'Drive upload failed: {up_resp.text}'}), 500

            result = up_resp.json()
            return jsonify({
                'success':  True,
                'fileName': file_name,
                'fileId':   result.get('id'),
            })

        except yt_dlp.utils.DownloadError as e:
            return jsonify({'error': f'Could not download: {str(e)}'}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500


@app.route('/trim-and-upload', methods=['POST'])
def trim_and_upload():
    """Download a direct video URL, trim with FFmpeg, upload to Drive."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    url          = data.get('url', '').strip()
    access_token = data.get('accessToken', '').strip()
    folder_id    = data.get('folderId', '')
    filename     = data.get('filename', 'trimmed.mp4')
    start_time   = data.get('startTime')   # seconds (float or None)
    end_time     = data.get('endTime')     # seconds (float or None)
    mute         = data.get('mute', False)

    if not url or not access_token:
        return jsonify({'error': 'url and accessToken required'}), 400

    # Ensure .mp4 extension
    if not filename.lower().endswith('.mp4'):
        filename = filename + '.mp4'

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            input_path  = os.path.join(tmpdir, 'input.mp4')
            output_path = os.path.join(tmpdir, filename)

            # 1. Download source video
            r = requests.get(url, stream=True, timeout=300,
                             headers={'User-Agent': 'Mozilla/5.0'})
            r.raise_for_status()
            with open(input_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

            # 2. Build FFmpeg command
            cmd = ['ffmpeg', '-y', '-i', input_path]
            if start_time is not None:
                cmd += ['-ss', str(float(start_time))]
            if end_time is not None:
                cmd += ['-to', str(float(end_time))]
            if mute:
                cmd.append('-an')
            cmd += ['-c', 'copy', output_path]

            result = __import__('subprocess').run(
                cmd, capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                return jsonify({'error': f'FFmpeg error: {result.stderr[-500:]}'}), 500

            file_size = os.path.getsize(output_path)

            # 3. Upload to Google Drive (resumable)
            metadata = {'name': filename}
            if folder_id:
                metadata['parents'] = [folder_id]

            init_resp = requests.post(
                'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable',
                headers={
                    'Authorization':           f'Bearer {access_token}',
                    'Content-Type':            'application/json',
                    'X-Upload-Content-Type':   'video/mp4',
                    'X-Upload-Content-Length': str(file_size),
                },
                json=metadata,
            )
            if not init_resp.ok:
                return jsonify({'error': f'Drive init failed: {init_resp.text}'}), 500

            upload_url = init_resp.headers.get('Location')
            with open(output_path, 'rb') as f:
                up_resp = requests.put(
                    upload_url,
                    data=f,
                    headers={
                        'Content-Type':   'video/mp4',
                        'Content-Length': str(file_size),
                    },
                )
            if not up_resp.ok:
                return jsonify({'error': f'Drive upload failed: {up_resp.text}'}), 500

            result_json = up_resp.json()
            return jsonify({
                'success':  True,
                'fileName': filename,
                'fileId':   result_json.get('id'),
            })

        except Exception as e:
            return jsonify({'error': str(e)}), 500


@app.route('/download-direct', methods=['POST'])
def download_direct():
    """Download a file from a direct URL (e.g. Pexels) and upload it to Drive."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    url          = data.get('url', '').strip()
    access_token = data.get('accessToken', '').strip()
    folder_id    = data.get('folderId', '')
    filename     = data.get('filename', 'file.mp4')
    mime_type    = data.get('mimeType', 'video/mp4')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not access_token:
        return jsonify({'error': 'No access token provided'}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            filepath = os.path.join(tmpdir, filename)
            # Download from direct URL
            r = requests.get(url, stream=True, timeout=300,
                             headers={'User-Agent': 'Mozilla/5.0'})
            r.raise_for_status()
            with open(filepath, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

            file_size = os.path.getsize(filepath)

            # Upload to Google Drive (resumable)
            metadata = {'name': filename}
            if folder_id:
                metadata['parents'] = [folder_id]

            init_resp = requests.post(
                'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable',
                headers={
                    'Authorization':           f'Bearer {access_token}',
                    'Content-Type':            'application/json',
                    'X-Upload-Content-Type':   mime_type,
                    'X-Upload-Content-Length': str(file_size),
                },
                json=metadata,
            )
            if not init_resp.ok:
                return jsonify({'error': f'Drive init failed: {init_resp.text}'}), 500

            upload_url = init_resp.headers.get('Location')
            with open(filepath, 'rb') as f:
                up_resp = requests.put(
                    upload_url,
                    data=f,
                    headers={
                        'Content-Type':   mime_type,
                        'Content-Length': str(file_size),
                    },
                )
            if not up_resp.ok:
                return jsonify({'error': f'Drive upload failed: {up_resp.text}'}), 500

            result = up_resp.json()
            return jsonify({
                'success':  True,
                'fileName': filename,
                'fileId':   result.get('id'),
            })

        except Exception as e:
            return jsonify({'error': str(e)}), 500


@app.route('/transcribe', methods=['POST'])
def transcribe():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    file_id      = data.get('fileId', '').strip()
    social_url   = data.get('socialUrl', '').strip()
    access_token = data.get('accessToken', '').strip()
    gemini_key   = data.get('geminiApiKey', '').strip()
    file_name    = data.get('fileName', 'video')
    mode         = data.get('mode', 'transcribe')  # 'transcribe' or 'analyze'

    if not gemini_key:
        return jsonify({'error': 'No Gemini API key — add it in Settings.'}), 400
    if not file_id and not social_url:
        return jsonify({'error': 'No fileId or socialUrl provided'}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # ── Step 1: Get the media file ──────────────────────────────────
            if file_id and access_token:
                local_path = os.path.join(tmpdir, 'media.mp4')
                drive_resp = requests.get(
                    f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
                    headers={'Authorization': f'Bearer {access_token}'},
                    stream=True
                )
                if not drive_resp.ok:
                    return jsonify({'error': f'Drive download failed ({drive_resp.status_code})'}), 500
                with open(local_path, 'wb') as f:
                    for chunk in drive_resp.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                mime_type = drive_resp.headers.get('Content-Type', 'video/mp4').split(';')[0].strip()

            else:
                # Download audio-only track from social URL (faster + smaller)
                ydl_opts = {
                    'outtmpl':     os.path.join(tmpdir, 'audio.%(ext)s'),
                    'format':      'bestaudio[ext=m4a]/bestaudio/best',
                    'noplaylist':  True,
                    'quiet':       True,
                    'no_warnings': True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([social_url])
                audio_files = glob.glob(os.path.join(tmpdir, 'audio.*'))
                if not audio_files:
                    return jsonify({'error': 'Could not download audio from URL'}), 500
                local_path = audio_files[0]
                ext = os.path.splitext(local_path)[1].lower()
                mime_map = {'.m4a': 'audio/mp4', '.mp3': 'audio/mpeg', '.webm': 'audio/webm', '.ogg': 'audio/ogg', '.mp4': 'video/mp4'}
                mime_type = mime_map.get(ext, 'audio/mp4')

            file_size = os.path.getsize(local_path)
            if file_size == 0:
                return jsonify({'error': 'Downloaded file is empty'}), 500

            # ── Step 2: Upload to Gemini File API (resumable) ───────────────
            start_resp = requests.post(
                f'https://generativelanguage.googleapis.com/upload/v1beta/files?key={gemini_key}',
                headers={
                    'X-Goog-Upload-Protocol':            'resumable',
                    'X-Goog-Upload-Command':             'start',
                    'X-Goog-Upload-Header-Content-Length': str(file_size),
                    'X-Goog-Upload-Header-Content-Type': mime_type,
                    'Content-Type':                      'application/json',
                },
                json={'file': {'display_name': file_name}}
            )
            if not start_resp.ok:
                return jsonify({'error': f'Gemini upload init failed: {start_resp.text}'}), 500

            upload_url = start_resp.headers.get('x-goog-upload-url')
            if not upload_url:
                return jsonify({'error': 'No upload URL returned by Gemini'}), 500

            with open(local_path, 'rb') as f:
                up_resp = requests.post(
                    upload_url,
                    headers={
                        'Content-Length':        str(file_size),
                        'X-Goog-Upload-Offset':  '0',
                        'X-Goog-Upload-Command': 'upload, finalize',
                    },
                    data=f
                )
            if not up_resp.ok:
                return jsonify({'error': f'Gemini file upload failed: {up_resp.text}'}), 500

            file_info       = up_resp.json()
            gemini_file_name = file_info.get('file', {}).get('name', '')
            file_uri         = file_info.get('file', {}).get('uri', '')
            if not file_uri:
                return jsonify({'error': 'No file URI returned by Gemini'}), 500

            # ── Step 3: Poll until file is ACTIVE ───────────────────────────
            for _ in range(40):
                status_resp = requests.get(
                    f'https://generativelanguage.googleapis.com/v1beta/{gemini_file_name}?key={gemini_key}'
                )
                state = status_resp.json().get('state', '')
                if state == 'ACTIVE':
                    break
                if state == 'FAILED':
                    return jsonify({'error': 'Gemini file processing failed'}), 500
                time.sleep(2)
            else:
                return jsonify({'error': 'Gemini file processing timed out'}), 500

            # ── Step 4: Transcribe or Analyze ────────────────────────────────
            if mode == 'analyze':
                prompt = (
                    'Watch and listen to this entire video carefully and provide a detailed analysis. Structure your response with these sections:\n\n'
                    '## Visual Description\n'
                    'Describe what you see: scenes, people, actions, text/graphics on screen, setting, and any notable visual elements.\n\n'
                    '## Audio & Sound\n'
                    'Describe all audio: background sounds, music (genre/mood), sound effects, ambient noise, and overall audio tone.\n\n'
                    '## Speech & Dialogue\n'
                    'Transcribe or summarize any spoken words, narration, or dialogue.\n\n'
                    '## Summary\n'
                    'A brief overall summary of what this video is about, its purpose, mood, and key takeaways.'
                )
            else:
                prompt = (
                    'Transcribe every word spoken in this video or audio file accurately. '
                    'If there are multiple speakers, label them Speaker 1, Speaker 2, etc. '
                    'Include timestamps every 30 seconds or at speaker changes using the format [0:30]. '
                    'Provide only the transcript — no commentary, no summaries, no preamble.'
                )

            # Pick the best available model dynamically
            preferred = [
                'gemini-2.5-flash',
                'gemini-2.5-pro',
                'gemini-2.0-flash-lite',
                'gemini-2.0-flash',
                'gemini-1.5-flash',
                'gemini-1.5-pro',
            ]
            chosen_model = None
            try:
                list_resp = requests.get(
                    f'https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}'
                )
                if list_resp.ok:
                    available = {m['name'].split('/')[-1] for m in list_resp.json().get('models', [])}
                    for m in preferred:
                        if m in available:
                            chosen_model = m
                            break
            except Exception:
                pass
            if not chosen_model:
                chosen_model = preferred[0]  # try best guess anyway

            gen_resp = requests.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{chosen_model}:generateContent?key={gemini_key}',
                json={
                    'contents': [{
                        'parts': [
                            {'fileData': {'mimeType': mime_type, 'fileUri': file_uri}},
                            {'text': prompt}
                        ]
                    }]
                }
            )

            # Clean up Gemini file
            try:
                requests.delete(f'https://generativelanguage.googleapis.com/v1beta/{gemini_file_name}?key={gemini_key}')
            except Exception:
                pass

            if not gen_resp.ok:
                err_msg = gen_resp.json().get('error', {}).get('message', gen_resp.text)
                return jsonify({'error': f'Transcription failed: {err_msg}'}), 500

            gen_data   = gen_resp.json()
            transcript = (
                gen_data.get('candidates', [{}])[0]
                .get('content', {})
                .get('parts', [{}])[0]
                .get('text', '')
            )
            if not transcript:
                return jsonify({'error': 'No transcript returned — the video may have no speech.'}), 500

            return jsonify({'success': True, 'transcript': transcript})

        except yt_dlp.utils.DownloadError as e:
            return jsonify({'error': f'Could not download from URL: {str(e)}'}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import requests
import os
import tempfile
import glob
import time
import threading
import uuid
import re
import subprocess
import shutil
import base64

# In-memory job store — safe with gthread single-worker model
jobs = {}  # job_id -> { status, transcript, error, step }

app = Flask(__name__)

CORS(app, origins=[
    'https://google-drive-folder-tool.vercel.app',
    'http://localhost:3000',
    'http://localhost:5500',
])

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


_BROWSER_HEADERS = {
    'User-Agent':      ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/124.0.0.0 Safari/537.36'),
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Sec-Fetch-Dest':  'document',
    'Sec-Fetch-Mode':  'navigate',
    'Sec-Fetch-Site':  'none',
}
_CT_TO_EXT = {
    'image/jpeg': '.jpg', 'image/jpg': '.jpg',
    'image/png': '.png', 'image/webp': '.webp',
    'image/gif': '.gif', 'image/heic': '.heic',
    'image/avif': '.avif',
}

def try_direct_image_download(url, tmpdir):
    """
    Fallback when yt-dlp cannot download a URL.
    Returns (filepath, error_message) — filepath is None on failure.
    """
    try:
        sess = requests.Session()
        sess.headers.update(_BROWSER_HEADERS)

        resp = sess.get(url, timeout=20, allow_redirects=True)
        if not resp.ok:
            return None, f'Page returned HTTP {resp.status_code}'

        ct = resp.headers.get('Content-Type', '').split(';')[0].strip()

        # Case 1: URL is already a direct image
        if ct.startswith('image/'):
            ext  = _CT_TO_EXT.get(ct, '.jpg')
            path = os.path.join(tmpdir, f'image{ext}')
            with open(path, 'wb') as f:
                f.write(resp.content)
            return path, None

        # Case 2: HTML page — search for image URL in meta tags and JSON data
        html = resp.text
        img_url = None

        # og:image / twitter:image meta tags (attribute order varies)
        for pat in [
            r'property=["\']og:image(?::url)?["\'][^>]*content=["\']([^"\']+)["\']',
            r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image(?::url)?["\']',
            r'name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'content=["\']([^"\']+)["\'][^>]*name=["\']twitter:image["\']',
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                img_url = m.group(1).replace('&amp;', '&').replace('\\/', '/')
                break

        # Facebook-specific: look for full-resolution image in their JSON blobs
        if not img_url:
            for pat in [
                r'"image":\{"uri":"(https://[^"]+scontent[^"]+\.(jpg|png|webp))',
                r'"display_url":"(https://[^"]+\.(jpg|png|webp)[^"]*)"',
                r'"src":"(https://[^"]+scontent[^"]+(?:jpg|png|webp)[^"]*)"',
            ]:
                m = re.search(pat, html)
                if m:
                    img_url = m.group(1).replace('\\u0026', '&').replace('\\/', '/')
                    break

        if not img_url:
            # Give a useful snippet so we can diagnose
            snippet = html[:500].replace('\n', ' ')
            return None, f'No image URL found in page. Page start: {snippet}'

        img_resp = sess.get(img_url, timeout=20, allow_redirects=True)
        if not img_resp.ok:
            return None, f'Image URL returned HTTP {img_resp.status_code}: {img_url[:100]}'

        img_ct = img_resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        if not img_ct.startswith('image/'):
            return None, f'Image URL did not return an image (got {img_ct}): {img_url[:100]}'

        ext  = _CT_TO_EXT.get(img_ct, '.jpg')
        path = os.path.join(tmpdir, f'image{ext}')
        with open(path, 'wb') as f:
            f.write(img_resp.content)
        return path, None

    except Exception as ex:
        return None, str(ex)


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
            filepath = None

            # --- Try yt-dlp first (handles YouTube, TikTok, Instagram video, etc.) ---
            try:
                ydl_opts = {
                    'outtmpl':             os.path.join(tmpdir, '%(title)s.%(ext)s'),
                    'format':              'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'merge_output_format': 'mp4',
                    'noplaylist':          True,
                    'quiet':               True,
                    'no_warnings':         True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)
                files = glob.glob(os.path.join(tmpdir, '*'))
                if files:
                    filepath = files[0]
            except yt_dlp.utils.DownloadError:
                pass  # will try fallback below

            # --- Fallback: og:image extraction (Facebook photos, Twitter images, etc.) ---
            fallback_err = None
            if not filepath:
                filepath, fallback_err = try_direct_image_download(url, tmpdir)

            if not filepath or not os.path.exists(filepath):
                detail = f' ({fallback_err})' if fallback_err else ''
                return jsonify({'error': (
                    f'Could not download this URL{detail}. '
                    'For Facebook/Instagram photos, try right-clicking the image → '
                    '"Open image in new tab" and paste that direct CDN URL instead.'
                )}), 400

            file_size = os.path.getsize(filepath)

            # Detect MIME type from the actual downloaded file's extension
            ext_to_mime = {
                '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
                '.avi': 'video/avi', '.mkv': 'video/x-matroska', '.m4v': 'video/x-m4v',
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                '.gif': 'image/gif', '.webp': 'image/webp', '.heic': 'image/heic',
                '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4',
            }
            actual_ext  = os.path.splitext(filepath)[1].lower()
            actual_mime = ext_to_mime.get(actual_ext, 'application/octet-stream')

            # Build final filename — respect custom name; use actual ext if no ext given
            if custom_name:
                has_ext = len(os.path.splitext(custom_name)[1]) > 1
                if has_ext:
                    file_name  = custom_name
                    custom_ext = os.path.splitext(custom_name)[1].lower()
                    file_mime  = ext_to_mime.get(custom_ext, actual_mime)
                else:
                    file_name = custom_name + actual_ext
                    file_mime = actual_mime
            else:
                file_name = os.path.basename(filepath)
                file_mime = actual_mime

            # --- Upload to Google Drive (resumable) ---
            metadata = {'name': file_name}
            if folder_id:
                metadata['parents'] = [folder_id]

            init_resp = requests.post(
                'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable',
                headers={
                    'Authorization':           f'Bearer {access_token}',
                    'Content-Type':            'application/json',
                    'X-Upload-Content-Type':   file_mime,
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
                        'Content-Type':   file_mime,
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


def run_transcribe_job(job_id, data):
    """Background thread: does the actual work and updates jobs[job_id]."""
    file_id      = data.get('fileId', '').strip()
    social_url   = data.get('socialUrl', '').strip()
    access_token = data.get('accessToken', '').strip()
    gemini_key   = data.get('geminiApiKey', '').strip()
    file_name    = data.get('fileName', 'video')
    mode         = data.get('mode', 'transcribe')

    def set_step(msg):
        jobs[job_id]['step'] = msg

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # ── Step 1: Get the media file ──────────────────────────────────
            set_step('Downloading media…')
            if file_id and access_token:
                local_path = os.path.join(tmpdir, 'media.mp4')
                # Retry up to 3 times — Drive sometimes needs a moment after upload
                for attempt in range(3):
                    drive_resp = requests.get(
                        f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
                        headers={'Authorization': f'Bearer {access_token}'},
                        stream=True
                    )
                    if drive_resp.ok:
                        with open(local_path, 'wb') as f:
                            for chunk in drive_resp.iter_content(chunk_size=1024 * 1024):
                                f.write(chunk)
                        # If file is suspiciously small, Drive may still be processing
                        if os.path.getsize(local_path) > 10240:  # > 10 KB
                            break
                    if attempt < 2:
                        set_step(f'Waiting for Drive to finish processing… (attempt {attempt + 2}/3)')
                        time.sleep(8)
                else:
                    sz = os.path.getsize(local_path) if os.path.exists(local_path) else 0
                    if not drive_resp.ok:
                        jobs[job_id] = {'status': 'error', 'error': f'Drive download failed ({drive_resp.status_code}). Make sure the file is shared or try again in a moment.'}
                    else:
                        jobs[job_id] = {'status': 'error', 'error': f'Drive returned a very small file ({sz} bytes). The video may still be processing — please wait 30 seconds and try again.'}
                    return
                mime_type = drive_resp.headers.get('Content-Type', 'video/mp4').split(';')[0].strip()
            else:
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
                    jobs[job_id] = {'status': 'error', 'error': 'Could not download audio from URL'}
                    return
                local_path = audio_files[0]
                ext = os.path.splitext(local_path)[1].lower()
                mime_map = {'.m4a': 'audio/mp4', '.mp3': 'audio/mpeg', '.webm': 'audio/webm', '.ogg': 'audio/ogg', '.mp4': 'video/mp4'}
                mime_type = mime_map.get(ext, 'audio/mp4')

            file_size = os.path.getsize(local_path)
            if file_size == 0:
                jobs[job_id] = {'status': 'error', 'error': 'Downloaded file is empty'}
                return

            # ── Step 2: Upload to Gemini File API ───────────────────────────
            set_step('Uploading to Gemini…')
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
                jobs[job_id] = {'status': 'error', 'error': f'Gemini upload init failed: {start_resp.text}'}
                return

            upload_url = start_resp.headers.get('x-goog-upload-url')
            if not upload_url:
                jobs[job_id] = {'status': 'error', 'error': 'No upload URL from Gemini'}
                return

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
                jobs[job_id] = {'status': 'error', 'error': f'Gemini file upload failed: {up_resp.text}'}
                return

            file_info        = up_resp.json()
            gemini_file_name = file_info.get('file', {}).get('name', '')
            file_uri         = file_info.get('file', {}).get('uri', '')
            if not file_uri:
                jobs[job_id] = {'status': 'error', 'error': 'No file URI from Gemini'}
                return

            # ── Step 3: Poll until ACTIVE ────────────────────────────────────
            set_step('Processing video…')
            for _ in range(40):
                status_resp = requests.get(
                    f'https://generativelanguage.googleapis.com/v1beta/{gemini_file_name}?key={gemini_key}'
                )
                state = status_resp.json().get('state', '')
                if state == 'ACTIVE':
                    break
                if state == 'FAILED':
                    jobs[job_id] = {'status': 'error', 'error': 'Gemini file processing failed'}
                    return
                time.sleep(2)
            else:
                jobs[job_id] = {'status': 'error', 'error': 'Gemini processing timed out'}
                return

            # ── Step 4: Transcribe / Analyze ─────────────────────────────────
            set_step('Running AI analysis…' if mode == 'analyze' else 'Transcribing…')
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

            preferred = ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.0-flash-lite', 'gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-1.5-pro']
            chosen_model = None
            try:
                list_resp = requests.get(f'https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}')
                if list_resp.ok:
                    available = {m['name'].split('/')[-1] for m in list_resp.json().get('models', [])}
                    for m in preferred:
                        if m in available:
                            chosen_model = m
                            break
            except Exception:
                pass
            if not chosen_model:
                chosen_model = preferred[0]

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
                jobs[job_id] = {'status': 'error', 'error': f'Transcription failed: {err_msg}'}
                return

            gen_data       = gen_resp.json()
            candidates     = gen_data.get('candidates', [])
            prompt_fb      = gen_data.get('promptFeedback', {})
            block_reason   = prompt_fb.get('blockReason', '')

            # Blocked at prompt level (e.g. SAFETY before any candidate is produced)
            if block_reason:
                jobs[job_id] = {'status': 'error', 'error': f'Gemini blocked this content (reason: {block_reason}). Try a different video.'}
                return

            # No candidates at all
            if not candidates:
                jobs[job_id] = {'status': 'error', 'error': 'Gemini returned no output for this video. It may be in an unsupported format, have no audio track, or be too short. Try a different video.'}
                return

            candidate  = candidates[0]
            finish     = candidate.get('finishReason', '')
            parts      = candidate.get('content', {}).get('parts', [])
            transcript = parts[0].get('text', '') if parts else ''

            if not transcript:
                if finish in ('SAFETY', 'RECITATION'):
                    jobs[job_id] = {'status': 'error', 'error': f'Gemini refused to process this video (reason: {finish}). Try a different video.'}
                elif finish == 'OTHER':
                    jobs[job_id] = {'status': 'error', 'error': 'Gemini could not read the video — it may be in an unsupported format or corrupt. Try re-exporting it as a standard MP4.'}
                elif finish == 'MAX_TOKENS':
                    jobs[job_id] = {'status': 'error', 'error': 'Video is too long for Gemini to fully process. Try a shorter clip.'}
                else:
                    jobs[job_id] = {'status': 'error', 'error': f'No text returned (finishReason: {finish or "unknown"}). The video may have no speech, be muted, or contain only music/ambient sound.'}
                return

            jobs[job_id] = {'status': 'done', 'transcript': transcript}

        except yt_dlp.utils.DownloadError as e:
            jobs[job_id] = {'status': 'error', 'error': f'Could not download from URL: {str(e)}'}
        except Exception as e:
            jobs[job_id] = {'status': 'error', 'error': str(e)}


@app.route('/transcribe', methods=['POST'])
def transcribe():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    file_id      = data.get('fileId', '').strip()
    social_url   = data.get('socialUrl', '').strip()
    gemini_key   = data.get('geminiApiKey', '').strip()

    if not gemini_key:
        return jsonify({'error': 'No Gemini API key — add it in Settings.'}), 400
    if not file_id and not social_url:
        return jsonify({'error': 'No fileId or socialUrl provided'}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {'status': 'pending', 'step': 'Starting…'}
    thread = threading.Thread(target=run_transcribe_job, args=(job_id, data), daemon=True)
    thread.start()
    return jsonify({'success': True, 'jobId': job_id})


@app.route('/transcribe-status/<job_id>', methods=['GET'])
def transcribe_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


def run_stitch_job(job_id, data):
    file_ids     = data.get('fileIds', [])
    access_token = data.get('accessToken', '')
    folder_id    = data.get('folderId', '')
    output_name  = data.get('outputName', 'stitched-video.mp4').strip()
    if not output_name.lower().endswith('.mp4'):
        output_name += '.mp4'

    def set_step(msg):
        jobs[job_id]['step'] = msg

    tmpdir = tempfile.mkdtemp()
    try:
        # 1. Download each video from Drive in order
        paths = []
        for i, file_id in enumerate(file_ids):
            set_step(f'Downloading video {i + 1} / {len(file_ids)}…')
            path = os.path.join(tmpdir, f'vid_{i:04d}.mp4')
            resp = requests.get(
                f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
                headers={'Authorization': f'Bearer {access_token}'},
                stream=True, timeout=300,
            )
            if not resp.ok:
                jobs[job_id] = {'status': 'error', 'error': f'Failed to download video {i + 1} (HTTP {resp.status_code})'}
                return
            with open(path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
            paths.append(path)

        # 2. Write concat file list
        set_step('Stitching videos together…')
        filelist = os.path.join(tmpdir, 'filelist.txt')
        with open(filelist, 'w') as f:
            for p in paths:
                f.write(f"file '{p}'\n")

        output_path = os.path.join(tmpdir, output_name)

        # 3. Try fast stream-copy concat first (no re-encoding)
        r = subprocess.run(
            ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', filelist,
             '-c', 'copy', output_path, '-y'],
            capture_output=True, timeout=600,
        )

        if r.returncode != 0:
            # Fall back to re-encode (handles mixed resolutions / codecs)
            set_step('Re-encoding to match formats (this may take a moment)…')
            r = subprocess.run(
                ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', filelist,
                 '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease,'
                        'pad=1920:1080:(ow-iw)/2:(oh-ih)/2',
                 '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                 '-c:a', 'aac', '-b:a', '128k',
                 output_path, '-y'],
                capture_output=True, timeout=600,
            )

        if r.returncode != 0:
            err = r.stderr.decode(errors='replace')[-300:]
            jobs[job_id] = {'status': 'error', 'error': f'FFmpeg failed: {err}'}
            return

        # 4. Upload stitched file to Drive
        set_step('Uploading stitched video to Drive…')
        file_size = os.path.getsize(output_path)
        metadata  = {'name': output_name}
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
            jobs[job_id] = {'status': 'error', 'error': f'Drive upload init failed: {init_resp.text}'}
            return

        upload_url = init_resp.headers.get('Location')
        with open(output_path, 'rb') as f:
            up_resp = requests.put(
                upload_url,
                data=f,
                headers={'Content-Type': 'video/mp4', 'Content-Length': str(file_size)},
                timeout=600,
            )
        if not up_resp.ok:
            jobs[job_id] = {'status': 'error', 'error': f'Drive upload failed: {up_resp.text}'}
            return

        drive_file = up_resp.json()
        jobs[job_id] = {
            'status':      'done',
            'driveFileId': drive_file.get('id'),
            'fileName':    output_name,
        }

    except subprocess.TimeoutExpired:
        jobs[job_id] = {'status': 'error', 'error': 'Stitch timed out — try fewer or shorter videos'}
    except Exception as e:
        jobs[job_id] = {'status': 'error', 'error': str(e)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route('/stitch', methods=['POST'])
def stitch():
    data = request.json or {}
    if not data.get('fileIds') or not data.get('accessToken'):
        return jsonify({'error': 'Missing fileIds or accessToken'}), 400
    job_id = str(uuid.uuid4())
    jobs[job_id] = {'status': 'pending', 'step': 'Starting…'}
    threading.Thread(target=run_stitch_job, args=(job_id, data), daemon=True).start()
    return jsonify({'success': True, 'jobId': job_id})


@app.route('/stitch-status/<job_id>', methods=['GET'])
def stitch_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


# ── Server-side ZIP download ────────────────────────────────────────────────
# Railway downloads from Drive at datacenter speeds (~1 Gbps), zips in memory,
# then streams the result to the browser as a single fast download.

def run_zip_job(job_id, file_ids, file_names, access_token, zip_filename):
    import zipfile
    tmpdir = tempfile.mkdtemp()
    zip_path = os.path.join(tmpdir, zip_filename)
    try:
        total = len(file_ids)
        # Download all files in parallel threads
        results = [None] * total
        errors  = []

        def fetch_file(idx, file_id, file_name):
            try:
                jobs[job_id]['step'] = f'Downloading {idx+1}/{total}: {file_name}'
                resp = requests.get(
                    f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
                    headers={'Authorization': f'Bearer {access_token}'},
                    stream=True, timeout=300
                )
                if not resp.ok:
                    errors.append(f'{file_name} ({resp.status_code})')
                    return
                # Detect extension from Content-Type
                ct = resp.headers.get('Content-Type', '').split(';')[0].strip()
                ext_map = {
                    'video/mp4':'mp4','video/quicktime':'mov','video/webm':'webm',
                    'video/x-matroska':'mkv','image/jpeg':'jpg','image/png':'png',
                    'image/gif':'gif','image/webp':'webp','audio/mpeg':'mp3',
                }
                detected_ext = ext_map.get(ct, '')
                name = file_name
                if detected_ext and '.' not in os.path.basename(name):
                    name += '.' + detected_ext
                data = b''.join(resp.iter_content(chunk_size=1024*1024))
                results[idx] = (name, data)
            except Exception as e:
                errors.append(f'{file_name}: {e}')

        threads = []
        for i, (fid, fname) in enumerate(zip(file_ids, file_names)):
            t = threading.Thread(target=fetch_file, args=(i, fid, fname), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        jobs[job_id]['step'] = 'Building ZIP…'
        # Deduplicate names inside ZIP
        seen = {}
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
            for item in results:
                if item is None:
                    continue
                name, data = item
                base, ext = os.path.splitext(name)
                final = name
                n = 2
                while final in seen:
                    final = f'{base} ({n}){ext}'
                    n += 1
                seen[final] = True
                zf.writestr(final, data)

        jobs[job_id] = {
            'status':   'done',
            'zip_path': zip_path,
            'filename': zip_filename,
            'tmpdir':   tmpdir,
            'errors':   errors,
        }
    except Exception as e:
        jobs[job_id] = {'status': 'error', 'error': str(e)}
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route('/zip', methods=['POST'])
def zip_start():
    data = request.json or {}
    file_ids   = data.get('fileIds', [])
    file_names = data.get('fileNames', [])
    access_token = data.get('accessToken', '')
    zip_filename = data.get('zipFilename', f'drive-files-{int(time.time())}.zip')
    if not file_ids or not access_token:
        return jsonify({'error': 'Missing fileIds or accessToken'}), 400
    job_id = str(uuid.uuid4())
    jobs[job_id] = {'status': 'pending', 'step': 'Starting…'}
    t = threading.Thread(
        target=run_zip_job,
        args=(job_id, file_ids, file_names, access_token, zip_filename),
        daemon=True
    )
    t.start()
    return jsonify({'jobId': job_id})


@app.route('/zip-status/<job_id>', methods=['GET'])
def zip_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    # Don't expose the filesystem path to the client
    return jsonify({k: v for k, v in job.items() if k not in ('zip_path', 'tmpdir')})


@app.route('/zip-download/<job_id>', methods=['GET'])
def zip_download(job_id):
    from flask import send_file
    job = jobs.get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Not ready'}), 404
    zip_path = job.get('zip_path')
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({'error': 'File gone'}), 404
    filename = job.get('filename', 'files.zip')
    # Clean up after 5 minutes
    def cleanup():
        time.sleep(300)
        shutil.rmtree(job.get('tmpdir', ''), ignore_errors=True)
        jobs.pop(job_id, None)
    threading.Thread(target=cleanup, daemon=True).start()
    return send_file(zip_path, as_attachment=True, download_name=filename)


# ── Scene screenshot extraction ─────────────────────────────────────────────
# Uses FFmpeg scene-change detection on Railway so the user doesn't need to
# download the full video themselves.

def run_extract_scenes_job(job_id, file_id, access_token):
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            jobs[job_id]['step'] = 'Downloading video…'
            resp = requests.get(
                f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
                headers={'Authorization': f'Bearer {access_token}'},
                stream=True, timeout=300
            )
            if not resp.ok:
                jobs[job_id] = {'status': 'error', 'error': f'Drive download failed ({resp.status_code})'}
                return

            video_path = os.path.join(tmpdir, 'video.mp4')
            with open(video_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)

            if os.path.getsize(video_path) == 0:
                jobs[job_id] = {'status': 'error', 'error': 'Downloaded file is empty'}
                return

            jobs[job_id]['step'] = 'Detecting scene changes…'
            frames_dir = os.path.join(tmpdir, 'frames')
            os.makedirs(frames_dir, exist_ok=True)

            # Always grab frame 0 (eq(n,0)) plus any frame where scene score > 0.25
            # showinfo writes pts_time to stderr so we can extract timestamps
            cmd = [
                'ffmpeg', '-i', video_path,
                '-vf', "select='eq(n\\,0)+gt(scene\\,0.25)',scale=640:-1,showinfo",
                '-vsync', 'vfr',
                '-q:v', '5',       # JPEG quality (2=best, 31=worst)
                '-frames:v', '30', # cap at 30 scenes
                os.path.join(frames_dir, 'frame%04d.jpg'),
                '-y'
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

            # Parse pts_time values from showinfo stderr output
            timestamps = []
            for line in result.stderr.split('\n'):
                m = re.search(r'pts_time:([\d.]+)', line)
                if m:
                    timestamps.append(float(m.group(1)))

            jobs[job_id]['step'] = 'Packaging screenshots…'
            frame_files = sorted(glob.glob(os.path.join(frames_dir, '*.jpg')))
            scenes = []
            for i, frame_path in enumerate(frame_files):
                with open(frame_path, 'rb') as f:
                    b64 = base64.b64encode(f.read()).decode('utf-8')
                ts = timestamps[i] if i < len(timestamps) else None
                scenes.append({'timestamp_sec': ts, 'image_b64': b64})

            jobs[job_id] = {'status': 'done', 'scenes': scenes, 'count': len(scenes)}

        except subprocess.TimeoutExpired:
            jobs[job_id] = {'status': 'error', 'error': 'Scene extraction timed out — try a shorter video'}
        except Exception as e:
            jobs[job_id] = {'status': 'error', 'error': str(e)}


@app.route('/extract-scenes', methods=['POST'])
def extract_scenes():
    data = request.json or {}
    file_id      = data.get('fileId', '').strip()
    access_token = data.get('accessToken', '').strip()
    if not file_id or not access_token:
        return jsonify({'error': 'Missing fileId or accessToken'}), 400
    job_id = str(uuid.uuid4())
    jobs[job_id] = {'status': 'pending', 'step': 'Starting…'}
    threading.Thread(
        target=run_extract_scenes_job,
        args=(job_id, file_id, access_token),
        daemon=True
    ).start()
    return jsonify({'jobId': job_id})


@app.route('/extract-scenes-status/<job_id>', methods=['GET'])
def extract_scenes_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

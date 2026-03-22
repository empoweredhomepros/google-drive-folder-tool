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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

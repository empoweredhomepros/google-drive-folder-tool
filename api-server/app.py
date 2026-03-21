from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import requests
import os
import tempfile
import glob

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
            file_name = os.path.basename(filepath)
            file_size = os.path.getsize(filepath)

            # Ensure .mp4 extension in the saved name
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

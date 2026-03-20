#!/usr/bin/env python3
"""
Google Drive Folder Tool
Lists files in a public Google Drive folder with shareable URLs.
Optionally rename files (requires OAuth credentials).
"""

import re
import sys
import json
import argparse
import urllib.request
import urllib.parse
import urllib.error


def extract_folder_id(url_or_id: str) -> str:
    """Extract folder ID from a Drive URL or return as-is if already an ID."""
    # Match folders/FOLDER_ID pattern
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # Assume it's already a raw ID
    return url_or_id.strip()


def api_get(url: str, api_key: str) -> dict:
    """Make a GET request to the Drive API."""
    sep = "&" if "?" in url else "?"
    full_url = f"{url}{sep}key={api_key}"
    try:
        with urllib.request.urlopen(full_url) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"\nHTTP {e.code} error: {body}", file=sys.stderr)
        sys.exit(1)


def api_patch(file_id: str, body: dict, access_token: str) -> dict:
    """Make a PATCH request to rename a file (requires OAuth)."""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"\nHTTP {e.code} error: {body}", file=sys.stderr)
        sys.exit(1)


def list_files(folder_id: str, api_key: str) -> list[dict]:
    """Return all files in a Drive folder (handles pagination)."""
    base = "https://www.googleapis.com/drive/v3/files"
    fields = "nextPageToken,files(id,name,mimeType,webViewLink,webContentLink)"
    q = urllib.parse.quote(f"'{folder_id}' in parents and trashed=false")
    files = []
    page_token = None

    while True:
        url = f"{base}?q={q}&fields={urllib.parse.quote(fields)}&pageSize=100"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        data = api_get(url, api_key)
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return files


def shareable_url(file: dict) -> str:
    """Return the best shareable URL for a file."""
    # webViewLink is the standard "open in Drive/Docs/Sheets/etc." link
    if file.get("webViewLink"):
        return file["webViewLink"]
    # Fallback for binary files
    if file.get("webContentLink"):
        return file["webContentLink"]
    # Manual fallback
    return f"https://drive.google.com/file/d/{file['id']}/view"


def print_files(files: list[dict]) -> None:
    """Pretty-print file list."""
    if not files:
        print("No files found in this folder.")
        return

    # Column widths
    max_name = max(len(f["name"]) for f in files)
    max_name = max(max_name, 4)  # at least "Name"

    header = f"{'#':<4}  {'Name':<{max_name}}  {'Type':<20}  URL"
    print(header)
    print("-" * min(len(header) + 40, 120))

    for i, f in enumerate(files, 1):
        mime = f.get("mimeType", "").replace("application/vnd.google-apps.", "").replace("application/", "")
        print(f"{i:<4}  {f['name']:<{max_name}}  {mime:<20}  {shareable_url(f)}")


def cmd_list(args):
    folder_id = extract_folder_id(args.folder)
    print(f"Fetching files from folder: {folder_id}\n")
    files = list_files(folder_id, args.api_key)
    print_files(files)

    if args.json:
        out = [{"name": f["name"], "id": f["id"], "url": shareable_url(f), "mimeType": f.get("mimeType")} for f in files]
        print("\nJSON output:")
        print(json.dumps(out, indent=2))


def cmd_rename(args):
    """Rename a file by its ID. Requires --access-token."""
    if not args.access_token:
        print("Error: --access-token is required for renaming files.", file=sys.stderr)
        print("Obtain one via: https://developers.google.com/oauthplayground", file=sys.stderr)
        sys.exit(1)

    result = api_patch(args.file_id, {"name": args.new_name}, args.access_token)
    print(f"Renamed to: {result.get('name')}")
    print(f"URL: {result.get('webViewLink', 'N/A')}")


def main():
    parser = argparse.ArgumentParser(
        description="Google Drive Folder Tool — list and rename files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List files in a public folder
  python drive_folder_tool.py list \\
    --folder "https://drive.google.com/drive/folders/FOLDER_ID" \\
    --api-key YOUR_API_KEY

  # List files and output JSON
  python drive_folder_tool.py list \\
    --folder FOLDER_ID --api-key YOUR_API_KEY --json

  # Rename a file (requires OAuth access token)
  python drive_folder_tool.py rename \\
    --file-id FILE_ID --new-name "My New Name.pdf" \\
    --access-token YOUR_OAUTH_TOKEN

How to get an API key (free, ~2 minutes):
  1. Go to https://console.cloud.google.com/
  2. Create or select a project
  3. Enable "Google Drive API"
  4. Go to APIs & Services > Credentials > Create Credentials > API key
  5. (Optional) Restrict the key to the Drive API

How to get an OAuth access token for renaming:
  1. Go to https://developers.google.com/oauthplayground
  2. Select "Drive API v3" > "https://www.googleapis.com/auth/drive"
  3. Authorize and copy the access token
""",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- list command ---
    p_list = sub.add_parser("list", help="List files in a Drive folder")
    p_list.add_argument("--folder", required=True, help="Drive folder URL or ID")
    p_list.add_argument("--api-key", required=True, help="Google Cloud API key")
    p_list.add_argument("--json", action="store_true", help="Also print JSON output")
    p_list.set_defaults(func=cmd_list)

    # --- rename command ---
    p_rename = sub.add_parser("rename", help="Rename a file")
    p_rename.add_argument("--file-id", required=True, help="Drive file ID to rename")
    p_rename.add_argument("--new-name", required=True, help="New file name")
    p_rename.add_argument("--access-token", required=True, help="OAuth 2.0 access token")
    p_rename.set_defaults(func=cmd_rename)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

"""Publish the health summary as ONE Google Doc, updated in place every day.

NotebookLM keeps a Google Doc source linked to its Drive file; updating the same file id (instead
of creating a new doc each day) means you add the source once and only re-sync it. Upload path:
Markdown -> HTML -> Drive import into a Google Doc (HTML -> Docs conversion is the long-standing,
well-supported import route).

Auth: OAuth for an installed (desktop) app, scope `drive.file` — the app only sees files it
created. One-time setup in docs/notebooklm.md:
    PULSO_GDRIVE_CLIENT=~/.pulso/google-client.json   (OAuth client of type "Desktop app")
    PULSO_GDRIVE_TOKEN=~/.pulso/google-token.json      (written on first run)
The doc id is remembered in ~/.pulso/google-doc-id.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

from pulso import config

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DOC_MIME = "application/vnd.google-apps.document"
TITLE = "Pulso — resumo de saúde e estresse"


def md_to_html(md: str) -> str:
    """Small Markdown subset -> HTML (headings, bold, lists, tables, paragraphs). Enough for the
    summary we generate; keeps the dependency list short."""
    out: list[str] = ["<html><head><meta charset='utf-8'></head><body>"]
    lines = md.splitlines()
    i = 0

    def inline(t: str) -> str:
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
        return t

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#"):
            level = min(6, len(ln) - len(ln.lstrip("#")))
            out.append(f"<h{level}>{inline(ln[level:].strip())}</h{level}>")
            i += 1
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            out.append("<table border='1' cellpadding='4'>")
            for k, r in enumerate(rows):
                tag = "th" if k == 0 else "td"
                out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in r) + "</tr>")
            out.append("</table>")
        elif ln.lstrip().startswith("- "):
            out.append("<ul>")
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                item = lines[i].lstrip()[2:]
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and not lines[i].lstrip().startswith("- "):
                    item += " " + lines[i].strip()
                    i += 1
                out.append(f"<li>{inline(item)}</li>")
            out.append("</ul>")
        elif ln.strip():
            para = [ln.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "- ")):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
        else:
            i += 1
    out.append("</body></html>")
    return "\n".join(out)


def _paths() -> tuple[Path, Path, Path]:
    h = config.home()
    client = Path(os.environ.get("PULSO_GDRIVE_CLIENT", h / "google-client.json")).expanduser()
    token = Path(os.environ.get("PULSO_GDRIVE_TOKEN", h / "google-token.json")).expanduser()
    return client, token, h / "google-doc-id"


def _service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'pulso[gdrive]' para publicar no Google Drive") from e
    client, token, _ = _paths()
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None  # e.g. refresh token expired (OAuth app in "testing" mode: 7 days)
    if not creds or not creds.valid:
        if not client.exists():
            raise SystemExit(f"cliente OAuth não encontrado em {client} — veja docs/notebooklm.md")
        flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
        creds = flow.run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token, 0o600)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def publish(md: str, service=None, title: str = TITLE) -> str:
    """Create the doc on first run, then update the same file. Returns its URL."""
    from googleapiclient.http import MediaInMemoryUpload

    svc = service or _service()
    _, _, id_file = _paths()
    media = MediaInMemoryUpload(md_to_html(md).encode("utf-8"), mimetype="text/html", resumable=False)
    file_id = id_file.read_text().strip() if id_file.exists() else None
    if file_id:
        try:
            svc.files().update(fileId=file_id, media_body=media, fields="id").execute()
            return f"https://docs.google.com/document/d/{file_id}/edit"
        except Exception:
            file_id = None  # deleted or not ours anymore: create a new one
    created = svc.files().create(body={"name": title, "mimeType": DOC_MIME}, media_body=media, fields="id").execute()
    file_id = created["id"]
    id_file.parent.mkdir(parents=True, exist_ok=True)
    id_file.write_text(file_id)
    return f"https://docs.google.com/document/d/{file_id}/edit"

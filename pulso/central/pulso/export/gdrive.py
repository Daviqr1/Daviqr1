"""Publish the health summary documents as Google Docs, each overwritten IN PLACE.

Why in place: Gemini Notebook (ex-NotebookLM) keeps Drive-imported Google Docs in sync on its own
(since May 2026, personal accounts included), and the Gemini app sees the same notebook. Deleting
and recreating a Doc drops it from the notebook — so the file id is sacred: we only recreate after
a 404 (you trashed it), and say so loudly.

Upload: Markdown -> HTML (with charset; tables documented for HTML import) -> Drive import into a
Google Doc. Auth: OAuth "Desktop app" client, scope drive.file only (non-sensitive: the app only
sees files it created). The background job never opens a browser — `pulso export --login` does,
once. Setup and gotchas (publish the OAuth app "In production" or the token dies in 7 days):
docs/gemini-notebook.md.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
from pathlib import Path

from pulso import config

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
DOC_MIME = "application/vnd.google-apps.document"
APP_KEY = "pulso"


class NeedsLogin(RuntimeError):
    pass


def scopes() -> list[str]:
    s = [DRIVE_SCOPE]
    if os.environ.get("PULSO_GCAL_ALERTS") == "1":
        s.append(CAL_SCOPE)
    return s


def _paths() -> tuple[Path, Path, Path]:
    h = config.home()
    client = Path(os.environ.get("PULSO_GDRIVE_CLIENT", h / "google-client.json")).expanduser()
    token = Path(os.environ.get("PULSO_GDRIVE_TOKEN", h / "google-token.json")).expanduser()
    return client, token, h / "google-docs.json"


def md_to_html(md: str) -> str:
    """Markdown subset we generate (headings, bold, code, lists, tables, paragraphs) -> HTML."""
    out: list[str] = ["<html><head><meta charset='utf-8'></head><body>"]
    lines = md.splitlines()
    i = 0

    def inline(t: str) -> str:
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
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
        elif ln.strip() == "---":
            out.append("<hr>")
            i += 1
        elif ln.strip():
            para = [ln.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "- ")) and lines[i].strip() != "---":
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
        else:
            i += 1
    out.append("</body></html>")
    return "\n".join(out)


def credentials(interactive: bool = False):
    try:
        import google.auth.exceptions
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'pulso[gdrive]' para publicar no Google Drive") from e
    client, token, _ = _paths()
    creds = Credentials.from_authorized_user_file(str(token), scopes()) if token.exists() else None
    if creds and not set(scopes()) <= set(creds.scopes or []):
        creds = None  # scopes changed (e.g. Calendar alerts turned on): log in again
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except google.auth.exceptions.RefreshError:
            creds = None  # revoked / expired (OAuth app left in "Testing": 7 days)
    if not creds or not creds.valid:
        if not interactive:
            raise NeedsLogin("Google: login necessário — rode `pulso export --login` no notebook")
        if not client.exists():
            raise SystemExit(f"cliente OAuth não encontrado em {client} — veja docs/gemini-notebook.md")
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(client), scopes())
        creds = flow.run_local_server(port=0, timeout_seconds=300)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token, 0o600)
    return creds


def service(interactive: bool = False):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=credentials(interactive), cache_discovery=False)


def _ids() -> dict[str, str]:
    _, _, f = _paths()
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


def _save_ids(ids: dict[str, str]) -> None:
    _, _, f = _paths()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(ids, indent=1))


def _media(md: str):
    from googleapiclient.http import MediaIoBaseUpload

    return MediaIoBaseUpload(io.BytesIO(md_to_html(md).encode("utf-8")), mimetype="text/html", resumable=False)


def _find(svc, key: str) -> str | None:
    """Recover a lost id: drive.file can list the files this app created."""
    q = f"appProperties has {{ key='{APP_KEY}' and value='{key}' }} and trashed = false"
    res = svc.files().list(q=q, spaces="drive", fields="files(id,modifiedTime)", orderBy="modifiedTime desc").execute(num_retries=3)
    files = res.get("files", [])
    return files[0]["id"] if files else None


def publish_doc(svc, key: str, title: str, md: str, ids: dict[str, str], log=print) -> str:
    from googleapiclient.errors import HttpError

    file_id = ids.get(key) or _find(svc, key)
    if file_id:
        try:
            svc.files().update(fileId=file_id, media_body=_media(md), fields="id,modifiedTime").execute(num_retries=3)
            ids[key] = file_id
            return file_id
        except HttpError as e:
            if getattr(e, "status_code", None) != 404 and getattr(getattr(e, "resp", None), "status", None) != 404:
                raise  # auth/quota/etc: never recreate (it would drop the source from the notebook)
            log(f"o Doc '{title}' sumiu do Drive; criando outro — adicione o novo no Gemini Notebook")
    created = svc.files().create(
        body={"name": title, "mimeType": DOC_MIME, "appProperties": {APP_KEY: key}},
        media_body=_media(md), fields="id,webViewLink",
    ).execute(num_retries=3)
    ids[key] = created["id"]
    return created["id"]


def publish_all(docs, svc=None, log=print) -> dict[str, str]:
    """Publish summary.Doc list; returns {key: url}."""
    svc = svc or service()
    ids = _ids()
    out = {}
    for d in docs:
        fid = publish_doc(svc, d.key, d.title, d.markdown, ids, log=log)
        out[d.key] = f"https://docs.google.com/document/d/{fid}/edit"
    _save_ids(ids)
    return out


def publish(md: str, service=None, title: str = "Pulso · Saúde") -> str:
    """Single-document variant (kept for the CLI/tests)."""
    from pulso.export.summary import Doc

    return publish_all([Doc("tudo", title, md)], svc=service)["tudo"]

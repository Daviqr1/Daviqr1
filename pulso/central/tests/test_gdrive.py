import pytest

pytest.importorskip("googleapiclient")

from pulso.export import gdrive  # noqa: E402


class FakeFiles:
    def __init__(self):
        self.calls = []
        self.fail_update = False

    def create(self, body, media_body, fields):
        self.calls.append(("create", body, media_body))
        return self

    def update(self, fileId, media_body, fields):
        if self.fail_update:
            raise RuntimeError("404")
        self.calls.append(("update", fileId, media_body))
        return self

    def execute(self):
        return {"id": "DOC123"}


class FakeService:
    def __init__(self):
        self.f = FakeFiles()

    def files(self):
        return self.f


def test_md_to_html_subset():
    h = gdrive.md_to_html("# T\n\nTexto **forte** e <x>.\n\n- a\n- b\n\n| c1 | c2 |\n|---|---|\n| 1 | 2 |\n")
    assert "<h1>T</h1>" in h and "<b>forte</b>" in h and "&lt;x&gt;" in h
    assert "<li>a</li>" in h and "<th>c1</th>" in h and "<td>2</td>" in h and "---" not in h


def test_publish_creates_once_then_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSO_HOME", str(tmp_path))
    svc = FakeService()
    url = gdrive.publish("# a", service=svc)
    assert url.endswith("/DOC123/edit") and svc.f.calls[0][0] == "create"
    assert svc.f.calls[0][1]["mimeType"] == gdrive.DOC_MIME
    gdrive.publish("# b", service=svc)
    assert svc.f.calls[1][:2] == ("update", "DOC123")
    svc.f.fail_update = True  # doc deleted by the user -> recreate
    gdrive.publish("# c", service=svc)
    assert svc.f.calls[-1][0] == "create"

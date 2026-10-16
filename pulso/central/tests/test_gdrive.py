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
            from googleapiclient.errors import HttpError

            resp = type("R", (), {"status": self.fail_update, "reason": "x"})()
            raise HttpError(resp, b"{}")
        self.calls.append(("update", fileId, media_body))
        return self

    def list(self, **kw):
        self.calls.append(("list", kw["q"]))
        self._list = True
        return self

    def execute(self, num_retries=0):
        if getattr(self, "_list", False):
            self._list = False
            return {"files": []}
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
    assert url.endswith("/DOC123/edit")
    assert [c[0] for c in svc.f.calls] == ["list", "create"]  # looks for a lost doc before creating
    assert svc.f.calls[1][1]["mimeType"] == gdrive.DOC_MIME
    gdrive.publish("# b", service=svc)
    assert svc.f.calls[2][:2] == ("update", "DOC123")
    svc.f.fail_update = 404  # doc deleted by the user -> recreate
    gdrive.publish("# c", service=svc)
    assert svc.f.calls[-1][0] == "create"
    svc.f.fail_update = 403  # auth/quota problems must NOT recreate (would drop the notebook source)
    with pytest.raises(Exception):
        gdrive.publish("# d", service=svc)
    assert svc.f.calls[-1][0] == "create" and sum(1 for c in svc.f.calls if c[0] == "create") == 2


def test_publish_all_three_docs_and_appproperties(tmp_path, monkeypatch):
    from pulso.export.summary import Doc

    monkeypatch.setenv("PULSO_HOME", str(tmp_path))
    svc = FakeService()
    urls = gdrive.publish_all([Doc("hoje", "Pulso · Saúde — Hoje", "# a"), Doc("dias", "D", "# b")], svc=svc)
    assert set(urls) == {"hoje", "dias"}
    creates = [c for c in svc.f.calls if c[0] == "create"]
    assert creates[0][1]["appProperties"] == {"pulso": "hoje"}
    # ids persisted: next run updates without listing
    svc2 = FakeService()
    svc2.f.calls.clear()
    gdrive.publish_all([Doc("hoje", "x", "# a")], svc=svc2)
    assert svc2.f.calls[0][0] == "update"


def test_needs_login_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSO_HOME", str(tmp_path))
    with pytest.raises(gdrive.NeedsLogin):
        gdrive.credentials(interactive=False)


def test_gcal_alert_event_shape(monkeypatch):
    from pulso.export import gcal

    seen = {}

    class Ev:
        def insert(self, calendarId, body):
            seen.update(body)
            return self

        def execute(self, num_retries=0):
            return {"id": seen["id"]}

    class Svc:
        def events(self):
            return Ev()

    eid = gcal.push({"id": "agora:29434567", "text": "Ativação alta há 10 min", "detail": "2,6 z"}, "https://docs/x", svc=Svc())
    assert eid.startswith("pulso") and set(eid) <= set("abcdefghijklmnopqrstuv0123456789")
    assert seen["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]}
    assert seen["transparency"] == "transparent" and "https://docs/x" in seen["description"]

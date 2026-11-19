"""Local and cloud models: MODELS.HOST, the local OpenAI-compatible endpoint, LOCKED answers stay in the project."""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from navigator_engineer import config, models
from navigator_engineer.projects import Projects


def _s(tmp_path, **m):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return config.Settings(workspace=ws, state_path=tmp_path / "state", models=m)


def _fake(answer="OSPF is fine.\n", seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.5:9b"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"<think>hmm</think>{answer}"}}],
                                         "usage": {"total_tokens": 10}})
    return httpx.Client(transport=httpx.MockTransport(handler))


LOCAL = {"URL": "http://127.0.0.1:11434/v1", "MODEL": "qwen3.5:9b"}


def test_host_kind(tmp_path, monkeypatch):
    monkeypatch.delenv("NAVIGATOR_HOST_MODEL", raising=False)
    assert models.host_kind(_s(tmp_path)) == "cloud"
    assert models.host_kind(_s(tmp_path, HOST="local")) == "local"
    assert models.host_kind(_s(tmp_path, HOST="weird")) == "cloud"
    monkeypatch.setenv("NAVIGATOR_HOST_MODEL", "local")
    assert models.host_kind(_s(tmp_path)) == "local"


def test_only_local_addresses(tmp_path):
    res = lambda ip: (lambda h, p: [(None, None, None, None, (ip, 0))])  # noqa: E731
    for url, ip, ok in [("http://127.0.0.1:11434/v1", "127.0.0.1", True), ("http://10.1.2.3:8000/v1", "10.1.2.3", True),
                        ("http://api.example.com/v1", "93.184.216.34", False)]:
        lm = models.LocalModel(_s(tmp_path, LOCAL={"URL": url, "MODEL": "m"}), client=_fake(), resolver=res(ip))
        if ok:
            assert lm.chat("t", "c")["text"] == "OSPF is fine."
        else:
            with pytest.raises(models.ModelError, match="not a local address"):
                lm.chat("t", "c")
    lm = models.LocalModel(_s(tmp_path, LOCAL={"URL": "http://gpu1.lab/v1", "MODEL": "m", "ALLOW_HOSTS": ["gpu1.lab"]}),
                           client=_fake(), resolver=res("93.184.216.34"))
    assert lm.chat("t", "c")["text"]
    with pytest.raises(models.ModelError, match="no local model set"):
        models.LocalModel(_s(tmp_path)).chat("t", "c")


def test_status(tmp_path):
    st = models.LocalModel(_s(tmp_path, LOCAL=LOCAL), client=_fake()).status()
    assert st["host_model"] == "cloud" and st["local"]["reachable"] and st["local"]["has_model"]
    assert models.LocalModel(_s(tmp_path)).status()["local"] == {"configured": False, "url": None, "model": None,
                                                                 "reachable": False}


def test_read_files_stays_in_the_project(tmp_path):
    p = tmp_path / "P"
    (p / "RAW").mkdir(parents=True)
    (p / "RAW" / "a.log").write_text("x" * 50)
    (p / "RAW" / "b.log").write_text("y" * 50)
    text, used, left = models.read_files(p, None, 110)
    assert left == ["RAW/b.log (cut)"] and used == ["RAW/a.log", "RAW/b.log"] and "MAX_INPUT_CHARS" in text
    with pytest.raises(models.ModelError, match="outside the project"):
        models.read_files(p, ["../../etc/passwd"], 1000)
    with pytest.raises(models.ModelError, match="not in the project"):
        models.read_files(p, ["NOPE"], 1000)


@pytest.mark.parametrize("mode,host,shown", [("LOCKED", "cloud", False), ("LOCKED", "local", True),
                                             ("OPEN", "cloud", True)])
def test_local_model_tool(tmp_path, monkeypatch, mode, host, shown):
    from navigator_engineer import server
    s = _s(tmp_path, HOST=host, LOCAL=LOCAL)
    monkeypatch.delenv("NAVIGATOR_HOST_MODEL", raising=False)
    monkeypatch.setattr(server, "_s", lambda: s)
    monkeypatch.setattr(config, "load", lambda: s)
    Projects(s).create(mode, "test", name="JOB1")
    pdir = s.workspace / "JOB1"
    (pdir / "RAW" / "show-ospf.txt").write_text("neighbor 10.0.0.2 FULL")
    seen = []
    monkeypatch.setattr(models.LocalModel, "_http", lambda self: _fake("All neighbors FULL.", seen))
    fn = getattr(server.local_model, "fn", server.local_model)
    r = fn("ask", project="JOB1", task="check the OSPF neighbors")
    assert r["answer_file"].startswith("OUT/LOCAL_MODEL_") and r["files_read"] == 1
    assert ("answer" in r) is shown and ("next" in r) is (not shown)
    saved = (pdir / r["answer_file"]).read_text()
    assert "All neighbors FULL." in saved and "RAW/show-ospf.txt" in saved
    body = json.loads(seen[-1].content)
    assert "neighbor 10.0.0.2 FULL" in body["messages"][1]["content"] and body["model"] == "qwen3.5:9b"
    nxt = Projects(s).show("JOB1")["next"]
    assert ("local_model action=ask" in nxt) is (mode == "LOCKED" and host == "cloud")


def test_settings_load_models(tmp_path, monkeypatch):
    f = tmp_path / "navigator.yml"
    f.write_text("PROJECT_ROOT: /tmp\nMODELS:\n  HOST: local\n  LOCAL: {URL: 'http://127.0.0.1:11434/v1', MODEL: m}\n")
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(f))
    s = config.load()
    assert s.models["HOST"] == "local" and models.local_settings(s)["MODEL"] == "m"
    assert dataclasses.replace(s, models={}).models == {}

"""The SSE endpoint streams real pipeline stages, then the answer in pieces that add up to exactly the JSON answer."""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import _chunks, app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def events(client, q):
    out = []
    with client.stream("POST", "/api/ask", json={"question": q}) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    for frame in body.strip().split("\n\n"):
        ev = next(l[6:].strip() for l in frame.split("\n") if l.startswith("event:"))
        data = json.loads(next(l[5:].strip() for l in frame.split("\n") if l.startswith("data:")))
        out.append((ev, data))
    return out


@pytest.mark.parametrize("q,stages", [
    ("What was RTPS's PLF in March 2025?", ["understanding", "querying", "answering"]),          # metric lookup
    ("Which units are down right now?", ["understanding", "querying", "answering"]),             # verified query
    ("What is PAF?", ["understanding", "answering"]),                                          # glossary
    ("What was ZZTPS generation in FY 2024-25?", ["understanding", "answering"]),              # refusal
])
def test_stream_matches_json(client, q, stages):
    evs = events(client, q)
    assert [d["stage"] for e, d in evs if e == "status"] == stages
    chunks = [d["text"] for e, d in evs if e == "answer_chunk"]
    assert len(chunks) > 3
    once = client.post("/api/ask_once", json={"question": q}).json()
    assert "".join(chunks) == once["answer"]
    names = [e for e, _ in evs]
    assert names[-1] == "done" and names.index("chart") > max(i for i, n in enumerate(names) if n == "answer_chunk")


@pytest.mark.parametrize("text", ["**Head.** a  b\n\n•  x — y\n•  z", "", "one", " lead\n", "a\n\nb"])
def test_chunks_round_trip(text):
    assert "".join(_chunks(text)) == text

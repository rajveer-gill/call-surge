"""Where a caller's sentence gets cut is Deepgram's decision, and it is tunable.

From a test call on 2026-09-02, one sentence arriving as two turns:

    00:04:22  caller_said  "Hi. This is Raj. I'd like to book a shampoo and a haircut"
    00:04:32  caller_said  "with Terrence on Thursday at 11AM."

Split at the breath before "with". The receptionist answered the first half — asking
which stylist he wanted — while the answer was still in flight. That is what Lana
reported as the AI only taking the first part of what she said.

endpointing was hardcoded at 300ms, which is shorter than an ordinary mid-sentence
pause. It also sits upstream of VOICE_DEEPGRAM_FINAL_DEBOUNCE_MS: raising that from 400
to 800 on the live service changed nothing, because Deepgram had already cut and sent
the transcript before our debounce was ever consulted.

Default stays 300 so no environment changes behaviour without someone deciding to.
"""

import pytest

from voice import deepgram_bridge
from voice.stt_config import deepgram_endpointing_ms

ENV = "VOICE_DEEPGRAM_ENDPOINTING_MS"


def test_the_default_is_unchanged(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert deepgram_endpointing_ms() == 300
    assert "endpointing=300" in deepgram_bridge.deepgram_listen_query()


def test_the_env_tunes_it(monkeypatch):
    monkeypatch.setenv(ENV, "800")
    assert deepgram_endpointing_ms() == 800
    assert "endpointing=800" in deepgram_bridge.deepgram_listen_query()
    assert "endpointing=300" not in deepgram_bridge.deepgram_listen_query()


def test_it_is_read_per_connection_not_at_import(monkeypatch):
    """The point of the lever is changing it without a deploy."""
    monkeypatch.setenv(ENV, "500")
    assert "endpointing=500" in deepgram_bridge.deepgram_listen_uri()
    monkeypatch.setenv(ENV, "1200")
    assert "endpointing=1200" in deepgram_bridge.deepgram_listen_uri()


@pytest.mark.parametrize("junk", ["", "abc", "800ms", "-"])
def test_junk_falls_back_rather_than_breaking_the_call(monkeypatch, junk):
    monkeypatch.setenv(ENV, junk)
    assert deepgram_endpointing_ms() == 300


def test_the_rest_of_the_query_is_untouched(monkeypatch):
    monkeypatch.setenv(ENV, "800")
    q = deepgram_bridge.deepgram_listen_query()
    for expected in (
        "model=nova-3",
        "encoding=mulaw",
        "sample_rate=8000",
        "channels=1",
        "smart_format=true",
        "interim_results=true",
    ):
        assert expected in q


def test_the_uri_still_points_at_deepgram(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert deepgram_bridge.deepgram_listen_uri().startswith(
        "wss://api.deepgram.com/v1/listen?"
    )

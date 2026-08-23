import json
import os
import sqlite3
import sys
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ai_agent import (
    generate_text_response,
    requires_archive_search,
    run_archive_agent,
)
from archive_search import ArchiveSearchEngine
from utils import migrate_db


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.call_id = call_id
        self.type = "function_call"
        self.name = name
        self.arguments = arguments

    def model_dump(self, exclude_none=True):
        return {
            "type": self.type,
            "call_id": self.call_id,
            "name": self.name,
            "arguments": self.arguments,
        }


class FakeOutputItem:
    def __init__(self, item_type, **data):
        self.type = item_type
        self.data = data

    def model_dump(self, exclude_none=True):
        return {"type": self.type, **self.data}


class FakeResponse:
    def __init__(self, *, content=None, tool_calls=None, output_items=None):
        self.output_text = content or ""
        self.output = list(output_items or []) + list(tool_calls or [])
        if content is not None:
            self.output.append(
                FakeOutputItem(
                    "message",
                    role="assistant",
                    content=[{"type": "output_text", "text": content}],
                )
            )


class FakeResponses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self.responses.pop(0)


def seeded_engine():
    conn = sqlite3.connect(":memory:")
    migrate_db(conn, conn.cursor())
    conn.execute(
        "INSERT INTO users(name, id, avatar, display_name) VALUES ('Giorgio', 'U1', '', 'Giorgio')"
    )
    conn.execute("INSERT INTO channels(name, id, is_private) VALUES ('dev', 'C1', 0)")
    conn.execute(
        """
        INSERT INTO messages(message, user, channel, timestamp, permalink, thread_ts)
        VALUES (?, 'U1', 'C1', '1600000000.1', ?, '1600000000.1')
        """,
        (
            "Idea: talk sugli incidents e outages",
            "https://sferait-ws.slack.com/archives/C1/p16000000001",
        ),
    )
    conn.commit()
    return conn, ArchiveSearchEngine(conn, requester_user_id="UREQUEST")


def test_simple_generation_uses_responses_reasoning_and_structured_text_format():
    responses = FakeResponses([FakeResponse(content='{"ok":true}')])
    client = SimpleNamespace(responses=responses)

    answer = generate_text_response(
        client,
        instructions="Return JSON",
        input_text="status",
        model="gpt-5.6-sol",
        reasoning_effort="low",
        max_output_tokens=100,
        text_format={"type": "json_object"},
    )

    assert answer == '{"ok":true}'
    assert responses.calls == [
        {
            "model": "gpt-5.6-sol",
            "instructions": "Return JSON",
            "input": "status",
            "reasoning": {"effort": "low"},
            "max_output_tokens": 100,
            "store": False,
            "text": {"format": {"type": "json_object"}},
        }
    ]


def test_failed_responses_are_raised_for_the_debug_pipeline():
    failed = FakeResponse()
    failed.status = "failed"
    failed.error = {"code": "server_error"}
    client = SimpleNamespace(responses=FakeResponses([failed]))

    with pytest.raises(RuntimeError, match="status=failed"):
        generate_text_response(
            client,
            instructions="test",
            input_text="test",
        )


def test_agent_returns_slack_and_sferaarchive_links_for_each_source():
    tool_call = FakeToolCall(
        "call_1",
        "grep_archive",
        '{"query":"talk incidents outages","match_mode":"all","limit":10}',
    )
    responses = FakeResponses(
        [
            FakeResponse(tool_calls=[tool_call]),
            FakeResponse(content="Ho trovato la proposta di Giorgio. [S1]"),
        ]
    )
    client = SimpleNamespace(responses=responses)
    conn, engine = seeded_engine()
    try:
        answer = run_archive_agent(
            client,
            question="Ricordi il talk sugli incident?",
            current_context="Giorgio: ne avevamo parlato",
            search_engine=engine,
        )
    finally:
        conn.close()

    assert "Ho trovato la proposta" in answer
    assert "*Fonti*" in answer
    assert "https://sferait-ws.slack.com/archives/C1" in answer
    assert "https://sferaarchive-client.vercel.app/" in answer
    assert "|Slack>" in answer
    assert "|SferaArchive>" in answer
    assert responses.calls[0]["model"] == "gpt-5.6-sol"
    assert responses.calls[0]["reasoning"] == {"effort": "medium"}
    assert responses.calls[0]["tool_choice"] == {
        "type": "function",
        "name": "grep_archive",
    }
    assert "temperature" not in responses.calls[0]
    assert responses.calls[0]["store"] is False
    assert responses.calls[0]["include"] == ["reasoning.encrypted_content"]
    tool_output = responses.calls[1]["input"][-1]
    assert tool_output["type"] == "function_call_output"
    assert tool_output["call_id"] == "call_1"
    assert "Idea: talk sugli incidents e outages" in tool_output["output"]
    assert '"archive_url"' in tool_output["output"]


def test_agent_reports_consulted_results_if_model_omits_citation_marker():
    tool_call = FakeToolCall(
        "call_1",
        "grep_archive",
        '{"query":"incidents","match_mode":"all","limit":10}',
    )
    responses = FakeResponses(
        [
            FakeResponse(tool_calls=[tool_call]),
            FakeResponse(content="Ho trovato un risultato, ma il modello non lo cita."),
        ]
    )
    client = SimpleNamespace(responses=responses)
    conn, engine = seeded_engine()
    try:
        answer = run_archive_agent(
            client,
            question="Cerca incidents",
            current_context="",
            search_engine=engine,
        )
    finally:
        conn.close()

    assert "*Risultati d'archivio consultati*" in answer
    assert "[S1] #dev" in answer


def test_agent_stops_after_bounded_rounds_and_forces_a_final_answer():
    repeating_call = lambda n: FakeResponse(
        tool_calls=[FakeToolCall(f"call_{n}", "grep_archive", '{"query":"missing"}')]
    )
    responses = FakeResponses(
        [
            repeating_call(1),
            repeating_call(2),
            FakeResponse(content="Non ho trovato prove sufficienti."),
        ]
    )
    client = SimpleNamespace(responses=responses)
    conn, engine = seeded_engine()
    try:
        answer = run_archive_agent(
            client,
            question="Trova qualcosa che non c'è",
            current_context="",
            search_engine=engine,
            max_rounds=2,
        )
    finally:
        conn.close()

    assert answer == "Non ho trovato una prova sufficiente nell'archivio visibile."
    assert responses.calls[-1]["tool_choice"] == "none"
    assert responses.calls[-1]["tools"]


def test_historical_question_cannot_return_an_uncorroborated_first_answer():
    tool_call = FakeToolCall(
        "call_1",
        "grep_archive",
        '{"query":"talk incidents outages","match_mode":"all"}',
    )
    responses = FakeResponses(
        [
            FakeResponse(content="Certo che me lo ricordo, fidati."),
            FakeResponse(tool_calls=[tool_call]),
            FakeResponse(content="Ho trovato la proposta nell'archivio. [S1]"),
        ]
    )
    client = SimpleNamespace(responses=responses)
    conn, engine = seeded_engine()
    try:
        answer = run_archive_agent(
            client,
            question="Ti ricordi quando parlavamo del talk sugli incidents?",
            current_context="",
            search_engine=engine,
        )
    finally:
        conn.close()

    assert "fidati" not in answer
    assert "proposta nell'archivio" in answer
    assert len(responses.calls) == 3


def test_invalid_source_markers_are_removed_from_the_answer():
    responses = FakeResponses(
        [FakeResponse(content="Risposta con fonte inventata [S999].")]
    )
    client = SimpleNamespace(responses=responses)
    conn, engine = seeded_engine()
    try:
        answer = run_archive_agent(
            client,
            question="Spiegami questo testo corrente",
            current_context="testo",
            search_engine=engine,
        )
    finally:
        conn.close()

    assert "S999" not in answer
    assert answer == "Risposta con fonte inventata."


def test_empty_historical_search_cannot_end_with_a_hallucinated_memory():
    tool_call = FakeToolCall(
        "call_1",
        "grep_archive",
        '{"query":"unicorno inesistente","match_mode":"all"}',
    )
    responses = FakeResponses(
        [
            FakeResponse(tool_calls=[tool_call]),
            FakeResponse(content="Certo, ricordo perfettamente quella conversazione."),
        ]
    )
    client = SimpleNamespace(responses=responses)
    conn, engine = seeded_engine()
    try:
        answer = run_archive_agent(
            client,
            question="Ricordi la conversazione sull'unicorno inesistente?",
            current_context="",
            search_engine=engine,
        )
    finally:
        conn.close()

    assert answer == "Non ho trovato una prova sufficiente nell'archivio visibile."
    assert "ricordo perfettamente" not in answer


def test_historical_query_detection_covers_the_reported_failure_shape():
    assert requires_archive_search(
        "Ti ricordi che ad un certo punto stavamo parlando di incidents e outages?"
    )
    assert requires_archive_search("Cerca nell'archivio chi ha detto questa cosa")
    assert not requires_archive_search("Riassumi questa conversazione corrente")
    assert not requires_archive_search("Come ottimizzo la memoria in Python?")


def test_responses_loop_preserves_reasoning_items_between_tool_turns():
    tool_call = FakeToolCall(
        "call_1",
        "grep_archive",
        '{"query":"incidents","match_mode":"all"}',
    )
    reasoning = FakeOutputItem(
        "reasoning",
        id="rs_1",
        encrypted_content="encrypted-reasoning",
    )
    responses = FakeResponses(
        [
            FakeResponse(tool_calls=[tool_call], output_items=[reasoning]),
            FakeResponse(content="Risultato [S1]"),
        ]
    )
    client = SimpleNamespace(responses=responses)
    conn, engine = seeded_engine()
    try:
        run_archive_agent(
            client,
            question="Cerca incidents",
            current_context="",
            search_engine=engine,
            reasoning_effort="high",
        )
    finally:
        conn.close()

    second_input = responses.calls[1]["input"]
    assert responses.calls[1]["reasoning"] == {"effort": "high"}
    assert any(
        item.get("type") == "reasoning"
        and item.get("encrypted_content") == "encrypted-reasoning"
        for item in second_input
    )


def test_real_openai_sdk_serializes_the_agent_request_to_responses_endpoint():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "instructions": None,
                "max_output_tokens": 3000,
                "model": "gpt-5.6-sol",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Risposta corrente.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "parallel_tool_calls": False,
                "previous_response_id": None,
                "reasoning": {"effort": "medium", "summary": None},
                "store": False,
                "temperature": 1,
                "text": {"format": {"type": "text"}},
                "tool_choice": "auto",
                "tools": [],
                "top_p": 1,
                "truncation": "disabled",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAI(
        api_key="dummy",
        base_url="https://example.test/v1",
        http_client=http_client,
    )
    conn, engine = seeded_engine()
    try:
        answer = run_archive_agent(
            client,
            question="Riassumi il contesto corrente",
            current_context="Alice: messaggio corrente",
            search_engine=engine,
        )
    finally:
        conn.close()
        client.close()

    assert answer == "Risposta corrente."
    assert captured["path"] == "/v1/responses"
    assert captured["body"]["reasoning"] == {"effort": "medium"}
    assert captured["body"]["store"] is False
    assert captured["body"]["tools"][0]["name"] == "grep_archive"
    assert captured["body"]["tools"][0]["type"] == "function"

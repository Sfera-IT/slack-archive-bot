import os
import sqlite3
import sys
from copy import deepcopy
from types import SimpleNamespace

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ai_agent import requires_archive_search, run_archive_agent
from archive_search import ArchiveSearchEngine
from utils import migrate_db


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments

    def model_dump(self, exclude_none=True):
        return {"name": self.name, "arguments": self.arguments}


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)

    def model_dump(self, exclude_none=True):
        return {
            "id": self.id,
            "type": self.type,
            "function": self.function.model_dump(),
        }


class FakeMessage:
    def __init__(self, *, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none=True):
        result = {"role": "assistant"}
        if self.content is not None:
            result["content"] = self.content
        if self.tool_calls:
            result["tool_calls"] = [call.model_dump() for call in self.tool_calls]
        return result


class FakeCompletions:
    def __init__(self, messages):
        self.responses = list(messages)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=self.responses.pop(0))])


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


def test_agent_iterates_over_archive_tool_and_returns_cited_slack_source():
    tool_call = FakeToolCall(
        "call_1",
        "grep_archive",
        '{"query":"talk incidents outages","match_mode":"all","limit":10}',
    )
    completions = FakeCompletions(
        [
            FakeMessage(tool_calls=[tool_call]),
            FakeMessage(content="Ho trovato la proposta di Giorgio. [S1]"),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
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
    assert completions.calls[0]["model"] == "gpt-5.6-sol"
    assert completions.calls[0]["reasoning_effort"] == "medium"
    assert completions.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "grep_archive"},
    }
    assert "temperature" not in completions.calls[0]
    tool_output = completions.calls[1]["messages"][-1]
    assert tool_output["role"] == "tool"
    assert "Idea: talk sugli incidents e outages" in tool_output["content"]


def test_agent_reports_consulted_results_if_model_omits_citation_marker():
    tool_call = FakeToolCall(
        "call_1",
        "grep_archive",
        '{"query":"incidents","match_mode":"all","limit":10}',
    )
    completions = FakeCompletions(
        [
            FakeMessage(tool_calls=[tool_call]),
            FakeMessage(content="Ho trovato un risultato, ma il modello non lo cita."),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
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
    repeating_call = lambda n: FakeMessage(
        tool_calls=[FakeToolCall(f"call_{n}", "grep_archive", '{"query":"missing"}')]
    )
    completions = FakeCompletions(
        [
            repeating_call(1),
            repeating_call(2),
            FakeMessage(content="Non ho trovato prove sufficienti."),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
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
    assert completions.calls[-1]["tool_choice"] == "none"
    assert completions.calls[-1]["tools"]


def test_historical_question_cannot_return_an_uncorroborated_first_answer():
    tool_call = FakeToolCall(
        "call_1",
        "grep_archive",
        '{"query":"talk incidents outages","match_mode":"all"}',
    )
    completions = FakeCompletions(
        [
            FakeMessage(content="Certo che me lo ricordo, fidati."),
            FakeMessage(tool_calls=[tool_call]),
            FakeMessage(content="Ho trovato la proposta nell'archivio. [S1]"),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
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
    assert len(completions.calls) == 3


def test_invalid_source_markers_are_removed_from_the_answer():
    completions = FakeCompletions(
        [FakeMessage(content="Risposta con fonte inventata [S999].")]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
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
    completions = FakeCompletions(
        [
            FakeMessage(tool_calls=[tool_call]),
            FakeMessage(content="Certo, ricordo perfettamente quella conversazione."),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
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

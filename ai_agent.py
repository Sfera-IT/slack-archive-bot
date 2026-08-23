"""Bounded GPT tool loop for evidence-first Slack archive answers."""

from __future__ import annotations

import json
import logging
import os
import re

from archive_search import ArchiveSearchEngine, EvidenceRegistry
from sferait_context import SFERAIT_SYSTEM_PROMPT

DEFAULT_AI_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "medium"
MAX_SEARCH_ROUNDS = 6
MAX_TOOL_CALLS = 8
MAX_OUTPUT_TOKENS = 3000

_HISTORICAL_QUERY_RE = re.compile(
    r"\b(?:"
    r"ricord\w*|(?:hai|avete|abbiamo)\s+memoria|archivi\w*|"
    r"(?:storic\w*|vecchi\w*)\s+(?:messagg\w*|post|thread|conversaz\w*)|"
    r"in\s+passat\w*|tempo\s+fa|"
    r"avev\w*\s+parlat\w*|parlav\w*|chi\s+(?:ha|aveva)\s+detto|"
    r"quando\s+.*(?:parlat\w*|discuss\w*|dett\w*|decis\w*)|"
    r"(?:abbiamo|si\s+era|si\s+è)\s+(?:parlat\w*|discuss\w*|decis\w*)|"
    r"cerca\w*|trova\w*|"
    r"(?:messagg\w*|post|thread|conversaz\w*)\s+precedent\w*|"
    r"remember\w*|archive\w*|"
    r"(?:histor\w*|previous\w*|old)\s+(?:message\w*|post|thread|conversation\w*)|"
    r"who\s+said"
    r")\b",
    re.IGNORECASE,
)

_FORCE_GREP_TOOL = {
    "type": "function",
    "name": "grep_archive",
}

logger = logging.getLogger(__name__)


ARCHIVE_TOOLS = [
    {
        "type": "function",
        "name": "grep_archive",
        "description": (
            "Cerca testo e metadati in tutti i messaggi archiviati dei canali "
            "Slack visibili all'utente. Usala più volte con sinonimi, nomi o "
            "varianti linguistiche quando la prima ricerca non basta."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Termini o frase da cercare.",
                },
                "match_mode": {
                    "type": "string",
                    "enum": ["all", "any", "phrase"],
                    "description": "all richiede tutti i termini; any almeno uno; phrase la frase esatta.",
                },
                "channel": {
                    "type": "string",
                    "description": "Nome o ID canale, oppure stringa vuota.",
                },
                "user": {
                    "type": "string",
                    "description": "Nome o ID autore, oppure stringa vuota.",
                },
                "after": {
                    "type": "string",
                    "description": "Data ISO minima, oppure stringa vuota.",
                },
                "before": {
                    "type": "string",
                    "description": "Data ISO massima, oppure stringa vuota.",
                },
                "sort": {
                    "type": "string",
                    "enum": ["relevance", "newest", "oldest"],
                    "description": "Ordine risultati; relevance è il default.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_thread",
        "description": "Legge cronologicamente il thread archiviato di un risultato trovato.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "thread_ts": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["channel_id", "thread_ts"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_surrounding",
        "description": (
            "Legge i messaggi cronologicamente vicini a un risultato, utile per "
            "conversazioni storiche non raccolte in un thread."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "message_ts": {"type": "string"},
                "before": {"type": "integer", "minimum": 0, "maximum": 20},
                "after": {"type": "integer", "minimum": 0, "maximum": 20},
            },
            "required": ["channel_id", "message_ts"],
            "additionalProperties": False,
        },
    },
]


def generate_text_response(
    client,
    *,
    instructions: str,
    input_text: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    text_format: dict | None = None,
) -> str:
    """Generate one stateless text response through the Responses API."""
    request = {
        "model": model or os.getenv("OPENAI_MODEL", DEFAULT_AI_MODEL),
        "instructions": instructions,
        "input": input_text,
        "reasoning": {
            "effort": reasoning_effort
            or os.getenv("OPENAI_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)
        },
        "max_output_tokens": int(max_output_tokens),
        "store": False,
    }
    if text_format is not None:
        request["text"] = {"format": text_format}
    response = client.responses.create(**request)
    _ensure_response_usable(response)
    return (getattr(response, "output_text", None) or "").strip()


def run_archive_agent(
    client,
    *,
    question: str,
    current_context: str,
    search_engine: ArchiveSearchEngine,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_rounds: int = MAX_SEARCH_ROUNDS,
) -> str:
    """Let GPT iteratively search the archive, then return one grounded answer."""
    model = model or os.getenv("OPENAI_MODEL", DEFAULT_AI_MODEL)
    reasoning_effort = reasoning_effort or os.getenv(
        "OPENAI_REASONING_EFFORT", DEFAULT_REASONING_EFFORT
    )
    input_items = [
        {
            "role": "user",
            "content": (
                "## Contesto Slack corrente\n"
                f"{current_context}\n\n"
                "## Richiesta dell'utente\n"
                f"{question}\n\n"
                "Se la richiesta riguarda fatti o conversazioni passate, cerca "
                "nell'archivio prima di rispondere."
            ),
        },
    ]
    tool_calls_used = 0
    successful_tool_calls = 0
    tool_errors: list[str] = []
    archive_search_required = requires_archive_search(question)

    for round_index in range(max(1, int(max_rounds))):
        tool_choice = (
            _FORCE_GREP_TOOL
            if archive_search_required and tool_calls_used == 0 and round_index == 0
            else "auto"
        )
        response = client.responses.create(
            model=model,
            instructions=SFERAIT_SYSTEM_PROMPT,
            input=input_items,
            tools=ARCHIVE_TOOLS,
            tool_choice=tool_choice,
            parallel_tool_calls=False,
            reasoning={"effort": reasoning_effort},
            max_output_tokens=MAX_OUTPUT_TOKENS,
            include=["reasoning.encrypted_content"],
            store=False,
        )
        _ensure_response_usable(response)
        input_items.extend(_response_output_dump(response))
        tool_calls = _response_function_calls(response)
        if not tool_calls:
            if archive_search_required and tool_calls_used == 0:
                input_items.append(
                    {
                        "role": "user",
                        "content": (
                            "La richiesta è storica: non rispondere per memoria o "
                            "deduzione. Esegui prima grep_archive."
                        ),
                    }
                )
                continue
            answer = (getattr(response, "output_text", None) or "").strip()
            return _finalize_answer(
                answer,
                search_engine.evidence,
                archive_search_required=archive_search_required,
                successful_tool_calls=successful_tool_calls,
                tool_errors=tool_errors,
            )

        budget_exhausted = False
        for tool_call in tool_calls:
            if tool_calls_used >= MAX_TOOL_CALLS:
                output = {
                    "error": "budget massimo di ricerca raggiunto",
                    "count": 0,
                    "results": [],
                }
                budget_exhausted = True
            else:
                output = _dispatch_tool(search_engine, tool_call)
                tool_calls_used += 1
                if output.get("error"):
                    tool_errors.append(str(output["error"]))
                else:
                    successful_tool_calls += 1
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                }
            )
        if budget_exhausted or tool_calls_used >= MAX_TOOL_CALLS:
            break

    if archive_search_required and tool_calls_used == 0:
        return (
            "Non sono riuscito a eseguire la ricerca nell'archivio visibile, "
            "quindi non posso confermare la conversazione richiesta."
        )

    input_items.append(
        {
            "role": "user",
            "content": (
                "Hai esaurito il budget di ricerca. Rispondi ora usando soltanto le "
                "evidenze recuperate; dichiara chiaramente ciò che resta incerto."
            ),
        }
    )
    response = client.responses.create(
        model=model,
        instructions=SFERAIT_SYSTEM_PROMPT,
        input=input_items,
        tools=ARCHIVE_TOOLS,
        tool_choice="none",
        parallel_tool_calls=False,
        reasoning={"effort": reasoning_effort},
        max_output_tokens=MAX_OUTPUT_TOKENS,
        include=["reasoning.encrypted_content"],
        store=False,
    )
    _ensure_response_usable(response)
    answer = (getattr(response, "output_text", None) or "").strip()
    return _finalize_answer(
        answer,
        search_engine.evidence,
        archive_search_required=archive_search_required,
        successful_tool_calls=successful_tool_calls,
        tool_errors=tool_errors,
    )


def render_sources(
    answer: str, evidence: EvidenceRegistry, *, fallback_limit: int = 5
) -> str:
    """Append Slack and durable SferaArchive links for cited evidence."""
    answer = (answer or "Non sono riuscito a produrre una risposta affidabile.").strip()
    answer = re.sub(
        r"\[(S\d+)\]",
        lambda match: (
            match.group(0) if evidence.get(match.group(1)) is not None else ""
        ),
        answer,
    )
    answer = re.sub(r"[ \t]+([,.;:!?])", r"\1", answer).strip()
    cited_ids = []
    for source_id in re.findall(r"\[(S\d+)\]", answer):
        if evidence.get(source_id) is not None and source_id not in cited_ids:
            cited_ids.append(source_id)

    if not cited_ids and evidence.ids():
        cited_ids = evidence.ids()[:fallback_limit]
        heading = "Risultati d'archivio consultati"
    else:
        heading = "Fonti"

    source_lines = []
    for source_id in cited_ids:
        hit = evidence.get(source_id)
        if hit is None:
            continue
        label = (
            f"[{source_id}] #{hit.channel_name} · {hit.user_name} · {hit.date_label}"
        )
        links = []
        if hit.permalink:
            links.append(f"<{hit.permalink}|Slack>")
        if hit.archive_url:
            links.append(f"<{hit.archive_url}|SferaArchive>")
        if links:
            source_lines.append(f"• {label} — " + " · ".join(links))
        else:
            source_lines.append(f"• {label} _(link non disponibile)_")

    if not source_lines:
        return answer
    return answer + f"\n\n*{heading}*\n" + "\n".join(source_lines)


def requires_archive_search(question: str) -> bool:
    """Detect questions that must not be answered from model memory alone."""
    normalized = re.sub(r"\s+", " ", str(question or "")).strip()
    return bool(_HISTORICAL_QUERY_RE.search(normalized))


def _finalize_answer(
    answer: str,
    evidence: EvidenceRegistry,
    *,
    archive_search_required: bool,
    successful_tool_calls: int,
    tool_errors: list[str],
) -> str:
    if archive_search_required and not evidence.ids():
        if successful_tool_calls:
            return "Non ho trovato una prova sufficiente nell'archivio visibile."
        if tool_errors:
            return (
                "Non sono riuscito a completare la ricerca nell'archivio visibile, "
                "quindi non posso confermare la conversazione richiesta."
            )
    return render_sources(answer, evidence)


def _dispatch_tool(search_engine: ArchiveSearchEngine, tool_call) -> dict:
    name = getattr(tool_call, "name", "")
    try:
        arguments = json.loads(getattr(tool_call, "arguments", "{}") or "{}")
    except json.JSONDecodeError:
        return {"error": "argomenti JSON non validi", "count": 0, "results": []}

    try:
        if name == "grep_archive":
            return search_engine.grep_archive(**arguments)
        if name == "read_thread":
            return search_engine.read_thread(**arguments)
        if name == "read_surrounding":
            return search_engine.read_surrounding(**arguments)
        return {"error": f"tool sconosciuto: {name}", "count": 0, "results": []}
    except (TypeError, ValueError) as exc:
        return {"error": f"argomenti non validi: {exc}", "count": 0, "results": []}
    except Exception:
        logger.exception("Archive tool %s failed", name)
        return {"error": "ricerca archivio non disponibile", "count": 0, "results": []}


def _response_function_calls(response) -> list:
    return [
        item
        for item in (getattr(response, "output", None) or [])
        if getattr(item, "type", None) == "function_call"
    ]


def _ensure_response_usable(response) -> None:
    status = str(getattr(response, "status", "completed") or "completed")
    if status not in {"failed", "cancelled"}:
        return
    error = getattr(response, "error", None)
    if hasattr(error, "model_dump"):
        error = error.model_dump(exclude_none=True)
    raise RuntimeError(f"OpenAI Responses status={status}: {error or 'nessun dettaglio'}")


def _response_output_dump(response) -> list[dict]:
    """Preserve every Responses output item for the next stateless turn."""
    dumped = []
    for item in getattr(response, "output", None) or []:
        if hasattr(item, "model_dump"):
            dumped.append(item.model_dump(exclude_none=True))
        elif isinstance(item, dict):
            dumped.append(dict(item))
        else:
            raise TypeError(f"unsupported Responses output item: {type(item).__name__}")
    return dumped

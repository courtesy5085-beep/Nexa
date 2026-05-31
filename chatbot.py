"""
chatbot.py — Streaming LLM + RAG + Tavily web search for Nexa.

Provider-agnostic:
  • OpenAI (chat completions, streaming)
  • Anthropic (messages stream)
  • Lovable AI Gateway (OpenAI-compatible)

Real-time data:
  • Tavily search for jobs/internships/scholarships/news
  • Results embedded and stored in `knowledge_chunks` for RAG

Slash commands:
  /jobs /internships /scholarships /news /update-profile
"""
from __future__ import annotations

import os
import re
import json
from typing import Generator, Iterable, Optional

import requests
import streamlit as st

import database as db


# ─── Config ──────────────────────────────────────────────────────────────────
def _cfg(key: str, default: str = "") -> str:
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


LLM_PROVIDER = _cfg("LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY = _cfg("OPENAI_API_KEY")
OPENAI_MODEL = _cfg("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_API_KEY = _cfg("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _cfg("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
LOVABLE_API_KEY = _cfg("LOVABLE_API_KEY")
LOVABLE_MODEL = _cfg("LOVABLE_MODEL", "google/gemini-3-flash-preview")
EMBEDDING_MODEL = _cfg("EMBEDDING_MODEL", "text-embedding-3-small")
TAVILY_API_KEY = _cfg("TAVILY_API_KEY")
RATE_LIMIT = int(_cfg("RATE_LIMIT_PER_10MIN", "30"))


# ─── Input sanitization ─────────────────────────────────────────────────────
MAX_INPUT_CHARS = 4000
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text: str) -> str:
    text = _CTRL_RE.sub("", text or "")
    return text.strip()[:MAX_INPUT_CHARS]


# ─── Rate limiting ──────────────────────────────────────────────────────────
def check_rate_limit(email: str) -> tuple[bool, int]:
    n = db.recent_message_count(email, minutes=10)
    return n < RATE_LIMIT, n


# ─── Embeddings ─────────────────────────────────────────────────────────────
def embed(text: str) -> Optional[list[float]]:
    if not OPENAI_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": EMBEDDING_MODEL, "input": text[:8000]},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"[embed] failed: {e}")
        return None


# ─── Tavily web search ──────────────────────────────────────────────────────
def tavily_search(query: str, max_results: int = 6, topic: str = "general") -> list[dict]:
    if not TAVILY_API_KEY:
        return []
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "advanced",
                "topic": topic,
                "max_results": max_results,
                "include_answer": False,
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception as e:
        print(f"[tavily] {e}")
        return []


def ingest_results(kind: str, results: list[dict]) -> int:
    """Embed + store results into knowledge_chunks. Returns count ingested."""
    n = 0
    for it in results:
        text = f"{it.get('title','')}\n{it.get('content','')}"
        vec = embed(text)
        if not vec:
            continue
        try:
            db.insert_knowledge_chunk(
                kind=kind,
                title=it.get("title", "")[:300],
                url=it.get("url", ""),
                content=it.get("content", "")[:8000],
                embedding=vec,
                metadata={"score": it.get("score")},
            )
            n += 1
        except Exception as e:
            print(f"[ingest] {e}")
    return n


# ─── RAG ────────────────────────────────────────────────────────────────────
def rag_context(query: str, kind: str | None = None, k: int = 5) -> str:
    vec = embed(query)
    if not vec:
        return ""
    hits = db.match_chunks(vec, k=k, kind=kind)
    if not hits:
        return ""
    lines = []
    for h in hits:
        lines.append(f"- [{h['title']}]({h['url']}) — {h['content'][:300]}…")
    return "Relevant context from knowledge base:\n" + "\n".join(lines)


# ─── Profile context ────────────────────────────────────────────────────────
def profile_summary(profile: dict | None) -> str:
    if not profile:
        return ""
    parts = []
    if profile.get("full_name"): parts.append(f"Name: {profile['full_name']}")
    if profile.get("education_level"): parts.append(f"Education: {profile['education_level']}")
    if profile.get("field_of_study"): parts.append(f"Field: {profile['field_of_study']}")
    if profile.get("location"): parts.append(f"Location: {profile['location']}")
    if profile.get("interests"): parts.append(f"Interests: {', '.join(profile['interests'])}")
    if profile.get("preferred_companies"): parts.append(f"Target companies: {', '.join(profile['preferred_companies'])}")
    return " | ".join(parts)


SYSTEM_PROMPT = """You are Nexa — an AI career & news companion.

Your job: help the user find real, current opportunities (jobs, internships,
scholarships) and stay informed on news & current affairs that match their
profile. Be concise, structured, and proactive.

Rules:
- Always personalize using the user profile when present.
- When you cite a result from web search or the knowledge base, include the
  source URL inline as a markdown link.
- Prefer bullet points + bold titles. Avoid filler.
- If asked something outside your scope, answer briefly but redirect to
  opportunities/news.
- Never fabricate URLs. If you don't have a source, say so.
"""


# ─── Command detection ──────────────────────────────────────────────────────
COMMAND_QUERIES = {
    "/jobs":          ("job",         "latest {field} jobs in {location} {interests}"),
    "/internships":   ("internship",  "latest {field} internships in {location} {interests}"),
    "/scholarships":  ("scholarship", "latest scholarships for {field} students {location}"),
    "/news":          ("news",        "latest news {interests} {field} {location}"),
}


def expand_command(cmd: str, profile: dict | None) -> tuple[str, str, str]:
    """Returns (kind, search_query, friendly_intro)."""
    kind, template = COMMAND_QUERIES[cmd]
    p = profile or {}
    q = template.format(
        field=p.get("field_of_study", "") or "",
        location=p.get("location", "") or "",
        interests=" ".join(p.get("interests", []) or []),
    ).strip()
    q = re.sub(r"\s+", " ", q) or kind
    intro = f"Pulling fresh **{kind}s** matching your profile…"
    return kind, q, intro


# ─── Streaming providers ────────────────────────────────────────────────────
def _stream_openai_compat(base_url: str, api_key: str, model: str, messages: list[dict]) -> Generator[str, None, None]:
    with requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages, "stream": True, "temperature": 0.4},
        stream=True,
        timeout=120,
    ) as r:
        if r.status_code == 429:
            yield "\n\n⚠️ Rate limited by provider. Try again in a minute."
            return
        if r.status_code == 402:
            yield "\n\n💳 Out of credits on the LLM provider."
            return
        if r.status_code >= 400:
            yield f"\n\n❌ LLM error ({r.status_code}): {r.text[:200]}"
            return
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            chunk = raw[6:].strip()
            if chunk == "[DONE]":
                return
            try:
                obj = json.loads(chunk)
                delta = obj["choices"][0].get("delta", {}).get("content")
                if delta:
                    yield delta
            except Exception:
                continue


def _stream_anthropic(model: str, messages: list[dict]) -> Generator[str, None, None]:
    # Anthropic expects system separately
    system = ""
    convo = []
    for m in messages:
        if m["role"] == "system":
            system += m["content"] + "\n"
        else:
            convo.append(m)
    with requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1500,
            "system": system or SYSTEM_PROMPT,
            "messages": convo,
            "stream": True,
        },
        stream=True,
        timeout=120,
    ) as r:
        if r.status_code >= 400:
            yield f"\n\n❌ Anthropic error ({r.status_code}): {r.text[:200]}"
            return
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            try:
                obj = json.loads(raw[6:])
            except Exception:
                continue
            if obj.get("type") == "content_block_delta":
                delta = obj.get("delta", {}).get("text")
                if delta:
                    yield delta


def stream_llm(messages: list[dict]) -> Generator[str, None, None]:
    if LLM_PROVIDER == "anthropic" and ANTHROPIC_API_KEY:
        yield from _stream_anthropic(ANTHROPIC_MODEL, messages)
    elif LLM_PROVIDER == "lovable" and LOVABLE_API_KEY:
        yield from _stream_openai_compat(
            "https://ai.gateway.lovable.dev/v1", LOVABLE_API_KEY, LOVABLE_MODEL, messages
        )
    elif OPENAI_API_KEY:
        yield from _stream_openai_compat(
            "https://api.openai.com/v1", OPENAI_API_KEY, OPENAI_MODEL, messages
        )
    else:
        yield "⚠️ No LLM provider configured. Set OPENAI_API_KEY, ANTHROPIC_API_KEY, or LOVABLE_API_KEY."


# ─── High-level chat orchestration ──────────────────────────────────────────
def build_messages(
    user_msg: str,
    history: list[dict],
    profile: dict | None,
    extra_context: str = "",
) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    p = profile_summary(profile)
    if p:
        msgs.append({"role": "system", "content": f"User profile → {p}"})
    if extra_context:
        msgs.append({"role": "system", "content": extra_context})
    for m in history[-12:]:  # last 12 turns
        if m["role"] in ("user", "assistant"):
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": user_msg})
    return msgs


def handle_turn(
    user_text: str,
    history: list[dict],
    profile: dict | None,
) -> Generator[str, None, None]:
    """Yields streaming response chunks. Handles commands + RAG + web search."""
    user_text = sanitize(user_text)
    if not user_text:
        yield "Please type a message."
        return

    extra_ctx = ""

    # Slash command path
    if user_text.startswith("/"):
        cmd = user_text.split()[0].lower()
        if cmd in COMMAND_QUERIES:
            kind, query, intro = expand_command(cmd, profile)
            yield intro + "\n\n"
            results = tavily_search(query, max_results=6,
                                    topic="news" if kind == "news" else "general")
            if results:
                ingest_results(kind, results)
                sources = "\n".join(
                    f"- **[{r['title']}]({r['url']})** — {r.get('content','')[:220]}…"
                    for r in results
                )
                extra_ctx = f"Fresh {kind} search results (use these, cite URLs):\n{sources}"
            else:
                extra_ctx = f"No fresh {kind} results from search. Use general knowledge."
            user_text = f"Summarize the top {kind}s above into a clean, ranked list with bold titles, 1-line description, and source links. Prioritize matches for my profile."

    # RAG augmentation for non-command queries that look like opportunity/news lookups
    elif any(k in user_text.lower() for k in ("job", "intern", "scholarship", "news", "opportunit")):
        ctx = rag_context(user_text, k=4)
        if ctx:
            extra_ctx = ctx

    messages = build_messages(user_text, history, profile, extra_ctx)
    yield from stream_llm(messages)

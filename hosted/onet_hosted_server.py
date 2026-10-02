# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2", "starlette", "uvicorn"]
# ///
"""
O*NET MCP Server — hosted (Streamable HTTP) variant.

Same two tools as the local stdio server (search_occupations,
get_occupation_report), reusing the same onet_search.py query logic, but
served over HTTP so it can run on Fly.io / Railway / any container host
instead of as a subprocess on one machine.

What this adds over the local server:
  - Streamable HTTP transport (mcp's `streamable_http_app()`) mounted in a
    Starlette app, so it's a normal web service with a /mcp endpoint.
  - A simple Bearer-API-key gate in front of /mcp. Keys come from the
    ONET_API_KEYS env var: "key1:CustomerA,key2:CustomerB". Swap
    `_lookup_key()` for a real database lookup when you move past the
    comma-separated-env-var stage (see README section "Growing past v1").
  - Lightweight usage logging to a local SQLite file (usage.db) — one row
    per authorized tool call, with the customer label and timestamp. This
    is NOT billing; it's the raw log you'd aggregate for metering/billing
    later (Stripe usage records, etc.).
  - /healthz for the host's health checks (unauthenticated).

Environment variables:
  ONET_DB_PATH   Path to onet.db (default: ../references/onet.db next to
                 this file). On Fly/Railway, point this at a mounted
                 persistent volume — don't bake a 200MB+ db into the image
                 if you can avoid it.
  ONET_API_KEYS  "key:label,key:label,..." — comma-separated API keys and
                 the customer label to log against each one.
  ONET_USAGE_DB  Path to the usage-log SQLite file (default: ./usage.db).
  PORT           Port to listen on (default: 8000; most hosts set this for
                 you).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import onet_search as onet  # noqa: E402

from mcp.server import MCPServer  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

# --- data path -------------------------------------------------------------

onet.DB_PATH = Path(
    os.environ.get("ONET_DB_PATH", str(SCRIPT_DIR.parent / "references" / "onet.db"))
)

# --- MCP server + tools (identical to the local server) --------------------

mcp = MCPServer("onet")


@mcp.tool()
def search_occupations(keyword: str) -> str:
    """
    Search O*NET occupations by keyword, job title, or O*NET-SOC code.

    Returns a Markdown table of matching occupations (code, title, and a
    short description) so you can pick the right O*NET-SOC code to pass to
    get_occupation_report. Use this first when you don't already know the
    exact code.
    """
    conn = onet._connect()
    try:
        results = onet._search_occupations(conn, keyword)
        if not results:
            return f"No occupations found matching '{keyword}'."
        return onet.format_list_markdown(results)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_report(
    query_or_code: str,
    format: str = "markdown",
    sections: list[str] | None = None,
) -> str:
    """
    Get a full O*NET occupation report: knowledge, skills, abilities, work
    activities, technology skills, tasks, education/training/experience,
    work styles, work values, work context, related occupations, alternate
    titles, emerging tasks, RIASEC interests, tools used, and detailed task
    ratings with confidence intervals (top 15 items per rated section).

    query_or_code: either a free-text search (e.g. "software developer") or
        an exact O*NET-SOC code (e.g. "15-1252.00"). A free-text query
        resolves to its best-matching occupation automatically.
    format: "markdown" (default, human-readable) or "json" (structured).
    sections: optionally restrict the report to specific sections, e.g.
        ["skills", "tasks", "related"]. Omit for the full report. Valid
        values: knowledge, skills, abilities, activities, technology, tasks,
        education, styles, values, context, related, titles, emerging_tasks,
        interests, tools, task_ratings.
    """
    conn = onet._connect()
    try:
        code = query_or_code.strip()
        looks_like_code = "-" in code and "." in code
        if not looks_like_code:
            results = onet._search_occupations(conn, code)
            if not results:
                return f"No occupations found matching '{query_or_code}'."
            code = str(results[0]["O*NET-SOC Code"])

        data = onet.gather_occupation_data(conn, code, sections)
        if not data:
            return f"No data found for O*NET-SOC code: {code}"

        if not onet._has_rated_data(conn, code):
            children = onet._find_child_codes(conn, code)
            if children:
                title = data.get("occupation", {}).get("title", code)
                note = (
                    f"\n\n> **Note:** {title} ({code}) is a parent category with no "
                    f"detailed skills/knowledge/abilities data of its own. Detailed "
                    f"data lives on its specialized child occupations:\n"
                    + "\n".join(f"> - `{c['code']}` {c['title']}" for c in children)
                )
                if format == "json":
                    return onet.format_json(data) + "\n\n" + note
                return onet.format_markdown(data) + note

        return onet.format_json(data) if format == "json" else onet.format_markdown(data)
    finally:
        conn.close()


# --- Granular per-section tools ---------------------------------------------
#
# Each of these is a one-section slice of get_occupation_report above, as a
# separate named tool. They all accept the same free-text-or-SOC-code input
# as get_occupation_report (resolved via onet.resolve_occupation) and share
# its query logic via onet.section_report, so there's one place (onet_search.py)
# that actually touches the database.


@mcp.tool()
def get_occupation_summary(query_or_code: str, format: str = "markdown") -> str:
    """
    Get basic identifying info for an occupation: title, description, and
    Job Zone (typical education/experience/training level). No detail
    sections — use the other get_occupation_* tools for those.
    """
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, [], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_knowledge(query_or_code: str, format: str = "markdown") -> str:
    """Get the knowledge domains required for an occupation (e.g. mathematics, computers, business), ranked by importance."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["knowledge"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_skills(query_or_code: str, format: str = "markdown") -> str:
    """Get the skills required for an occupation (e.g. critical thinking, programming), ranked by importance."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["skills"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_abilities(query_or_code: str, format: str = "markdown") -> str:
    """Get the abilities required for an occupation (e.g. deductive reasoning, manual dexterity), ranked by importance."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["abilities"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_work_activities(query_or_code: str, format: str = "markdown") -> str:
    """Get the general work activities for an occupation (e.g. analyzing data, coordinating with others), ranked by importance."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["activities"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_technology(query_or_code: str, format: str = "markdown") -> str:
    """Get the software and technology tools used in an occupation, flagging hot/in-demand technologies."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["technology"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_tasks(query_or_code: str, format: str = "markdown") -> str:
    """Get the core and supplemental task statements for an occupation."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["tasks"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_education(query_or_code: str, format: str = "markdown") -> str:
    """Get education, training, and experience requirements for an occupation, including the ETA survey breakdown by education level."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["education"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_work_styles(query_or_code: str, format: str = "markdown") -> str:
    """Get the work styles (personal attributes like attention to detail, dependability) associated with an occupation."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["styles"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_work_values(query_or_code: str, format: str = "markdown") -> str:
    """Get the work values (what the occupation offers, e.g. achievement, independence) for an occupation."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["values"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_work_context(query_or_code: str, format: str = "markdown") -> str:
    """Get the physical and social work context conditions for an occupation (e.g. indoors, face-to-face contact)."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["context"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_related(query_or_code: str, format: str = "markdown") -> str:
    """Get occupations related to the given one, ranked by relatedness tier."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["related"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_alternate_titles(query_or_code: str, format: str = "markdown") -> str:
    """Get alternate job titles reported for an occupation."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["titles"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_emerging_tasks(query_or_code: str, format: str = "markdown") -> str:
    """Get emerging tasks being added to an occupation — new responsibilities not yet in the core task list."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["emerging_tasks"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_interests(query_or_code: str, format: str = "markdown") -> str:
    """Get the RIASEC occupational interest profile for an occupation."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["interests"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_tools_used(query_or_code: str, format: str = "markdown") -> str:
    """Get the physical tools and equipment used in an occupation."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["tools"], format)
    finally:
        conn.close()


@mcp.tool()
def get_occupation_task_ratings(query_or_code: str, format: str = "markdown") -> str:
    """Get detailed task frequency/importance ratings with 95% confidence intervals for an occupation's top tasks."""
    conn = onet._connect()
    try:
        return onet.section_report(conn, query_or_code, ["task_ratings"], format)
    finally:
        conn.close()


# --- API keys ----------------------------------------------------------------


def _lookup_key(token: str) -> str | None:
    """
    Return the customer label for a valid API key, or None if invalid.

    v1: reads ONET_API_KEYS="key:label,key:label,...". Growing past this:
    replace the body with a lookup against a real customers table (Postgres,
    etc.) keyed by hashed API key, and check plan/quota there too.
    """
    raw = os.environ.get("ONET_API_KEYS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, label = pair.partition(":")
        if key == token:
            return label or "unknown"
    return None


# --- usage logging -------------------------------------------------------------

USAGE_DB_PATH = Path(os.environ.get("ONET_USAGE_DB", str(SCRIPT_DIR / "usage.db")))


def _init_usage_db() -> None:
    conn = sqlite3.connect(str(USAGE_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            customer TEXT NOT NULL,
            path TEXT NOT NULL,
            status INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _log_usage(customer: str, path: str, status: int) -> None:
    try:
        conn = sqlite3.connect(str(USAGE_DB_PATH))
        conn.execute(
            "INSERT INTO usage_log (ts, customer, path, status) VALUES (?, ?, ?, ?)",
            (time.time(), customer, path, status),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # logging must never break a request


# --- auth + logging middleware ------------------------------------------------


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Gates everything under /mcp behind a Bearer API key."""

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        token = auth_header[len("Bearer "):].strip() if auth_header.startswith("Bearer ") else ""

        customer = _lookup_key(token) if token else None
        if not customer:
            _log_usage(customer or "anonymous", request.url.path, 401)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        response = await call_next(request)
        _log_usage(customer, request.url.path, response.status_code)
        return response


# --- app -----------------------------------------------------------------------


async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_app() -> Starlette:
    _init_usage_db()
    inner = mcp.streamable_http_app(stateless_http=True)
    inner.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
    inner.add_middleware(ApiKeyMiddleware)
    return inner


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

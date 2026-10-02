# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp"]
# ///
"""
O*NET MCP Server

Exposes the O*NET occupational database (references/onet.db, ~1,000 occupations)
as MCP tools so Claude Desktop can query it directly on this machine, without ever
uploading the database (it's ~200MB, far over Claude's ~30MB skill/file upload cap).

Reuses the query logic in onet_search.py (same folder) rather than duplicating it.

Setup:
    pip install mcp

Then point Claude Desktop's config at this file (see README section added
alongside this file, or the chat instructions that shipped it).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make onet_search.py (same directory) importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import onet_search as onet  # noqa: E402

from mcp.server import MCPServer  # noqa: E402

mcp = MCPServer("onet")


@mcp.tool()
def search_occupations(keyword: str) -> str:
    """
    Search O*NET occupations by keyword, job title, or O*NET-SOC code.

    Returns a Markdown table of matching occupations (code, title, and a short
    description) so you can pick the right O*NET-SOC code to pass to
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


if __name__ == "__main__":
    mcp.run()

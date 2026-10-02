# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
O*NET Database Search Tool

Search the O*NET occupational database and generate Markdown or JSON reports.
Queries the SQLite database at references/onet.db (built by onet_build_db.py).

Usage:
    uv run scripts/onet_search.py "software developer"
    uv run scripts/onet_search.py --code 15-1252.00 --format json -o report.json
    uv run scripts/onet_search.py --list "engineer"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"
DB_PATH = REFERENCES_DIR / "onet.db"

SECTION_KEYS = [
    "knowledge",
    "skills",
    "abilities",
    "activities",
    "technology",
    "tasks",
    "education",
    "styles",
    "values",
    "context",
    "related",
    "titles",
    "emerging_tasks",
    "interests",
    "tools",
    "task_ratings",
]


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(
            f"Error: Database not found: {DB_PATH}\n"
            f"Run: uv run scripts/onet_update.py\n"
            f"Or:  uv run scripts/onet_build_db.py",
            file=sys.stderr,
        )
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _pick_table(conn: sqlite3.Connection, *names: str) -> str | None:
    tables = _existing_tables(conn)
    for name in names:
        if name in tables:
            return name
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _search_occupations(conn: sqlite3.Connection, keyword: str) -> list[dict[str, Any]]:
    # FTS5 search on occupation titles/descriptions with BM25 ranking
    fts_query = keyword.replace('"', '""')

    results: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    # Try exact SOC code match first (before FTS5, which doesn't handle codes well)
    if keyword.replace(".", "").replace("-", "").isdigit() or (
        "-" in keyword and "." in keyword
    ):
        exact_code = keyword.strip()
        exact_row = conn.execute(
            "SELECT o_net_soc_code, title, description FROM occupation_data WHERE o_net_soc_code = ?",
            (exact_code,),
        ).fetchone()
        if exact_row:
            code = exact_row["o_net_soc_code"]
            seen_codes.add(code)
            results.append({
                "O*NET-SOC Code": code,
                "Title": exact_row["title"],
                "Description": exact_row["description"],
            })
            return results  # Return early if exact match found

    # Primary: FTS5 match on occupation_data (title + description)
    rows = conn.execute(
        """
        SELECT o_net_soc_code, title, description, rank
        FROM occupation_fts
        WHERE occupation_fts MATCH ?
        ORDER BY rank
        LIMIT 50
        """,
        (fts_query,),
    ).fetchall()

    for r in rows:
        code = r["o_net_soc_code"]
        if code not in seen_codes:
            seen_codes.add(code)
            results.append({
                "O*NET-SOC Code": code,
                "Title": r["title"],
                "Description": r["description"],
            })

    # Secondary: FTS5 match on job/alternate titles for broader coverage
    if "alternate_titles_fts" in _existing_tables(conn):
        alt_rows = conn.execute(
            """
            SELECT DISTINCT o_net_soc_code
            FROM alternate_titles_fts
            WHERE alternate_titles_fts MATCH ?
            ORDER BY rank
            LIMIT 50
            """,
            (fts_query,),
        ).fetchall()

        for r in alt_rows:
            code = r["o_net_soc_code"]
            if code not in seen_codes:
                seen_codes.add(code)
                occ = conn.execute(
                    "SELECT * FROM occupation_data WHERE o_net_soc_code = ?",
                    (code,),
                ).fetchone()
                if occ:
                    results.append({
                        "O*NET-SOC Code": occ["o_net_soc_code"],
                        "Title": occ["title"],
                        "Description": occ["description"],
                    })

    # Fallback: LIKE search if FTS5 returns nothing (handles partial codes, etc.)
    if not results:
        like_pattern = f"%{keyword}%"
        rows = conn.execute(
            """
            SELECT o_net_soc_code, title, description FROM occupation_data
            WHERE title LIKE ? OR description LIKE ? OR o_net_soc_code LIKE ?
            ORDER BY
                CASE
                    WHEN LOWER(title) = LOWER(?) THEN 0
                    WHEN LOWER(title) LIKE LOWER(?) THEN 1
                    ELSE 2
                END,
                title
            LIMIT 50
            """,
            (like_pattern, like_pattern, like_pattern, keyword, like_pattern),
        ).fetchall()
        for r in rows:
            results.append({
                "O*NET-SOC Code": r["o_net_soc_code"],
                "Title": r["title"],
                "Description": r["description"],
            })

    return results


def _get_by_code(conn: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM occupation_data WHERE o_net_soc_code = ?",
        (code.strip(),),
    ).fetchone()
    return dict(row) if row else None


def _top_rated(
    conn: sqlite3.Connection,
    table: str,
    code: str,
    scale_id: str = "IM",
    n: int = 15,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT * FROM "{table}"
        WHERE o_net_soc_code = ? AND scale_id = ?
            AND (recommend_suppress IS NULL OR recommend_suppress != 'Y')
            AND data_value IS NOT NULL
        ORDER BY CAST(data_value AS REAL) DESC
        LIMIT ?
        """,
        (code, scale_id, n),
    ).fetchall()
    return _rows_to_dicts(rows)


def _has_rated_data(conn: sqlite3.Connection, code: str) -> bool:
    for table in ("skills", "essential_skills", "transferable_skills", "knowledge", "abilities"):
        if _pick_table(conn, table) is None:
            continue
        row = conn.execute(
            f'SELECT 1 FROM "{table}" WHERE o_net_soc_code = ? LIMIT 1',
            (code,),
        ).fetchone()
        if row:
            return True
    return False


def _find_child_codes(conn: sqlite3.Connection, code: str) -> list[dict[str, str]]:
    if not code.endswith(".00"):
        return []
    prefix = code[:-2]
    rows = conn.execute(
        "SELECT o_net_soc_code, title FROM occupation_data "
        "WHERE o_net_soc_code LIKE ? AND o_net_soc_code != ? "
        "ORDER BY o_net_soc_code",
        (f"{prefix}%", code),
    ).fetchall()
    return [{"code": r["o_net_soc_code"], "title": r["title"]} for r in rows]


def _get_emerging_tasks(conn: sqlite3.Connection, code: str) -> list[dict[str, Any]]:
    if _pick_table(conn, "emerging_tasks") is None:
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM emerging_tasks
        WHERE o_net_soc_code = ?
        ORDER BY date DESC
        """,
        (code,),
    ).fetchall()
    return _rows_to_dicts(rows)


def _get_interests(conn: sqlite3.Connection, code: str) -> list[dict[str, Any]]:
    table = _pick_table(conn, "career_interest_types", "interests")
    if table is None:
        return []
    rows = conn.execute(
        f"""
        SELECT element_id, element_name, scale_name, data_value, date, domain_source
        FROM "{table}"
        WHERE o_net_soc_code = ?
        ORDER BY CAST(data_value AS REAL) DESC
        """,
        (code,),
    ).fetchall()
    return _rows_to_dicts(rows)


def _get_tools_used(conn: sqlite3.Connection, code: str) -> list[dict[str, Any]]:
    if _pick_table(conn, "tools_used") is None:
        return []
    rows = conn.execute(
        """
        SELECT example, commodity_title
        FROM tools_used
        WHERE o_net_soc_code = ?
        ORDER BY commodity_title, example
        """,
        (code,),
    ).fetchall()
    return _rows_to_dicts(rows)


def _get_task_ratings(
    conn: sqlite3.Connection, code: str, n: int = 15
) -> list[dict[str, Any]]:
    # Get the single highest-relevance task from each task_id (ignoring per-category breakdowns)
    rows = conn.execute(
        """
        SELECT tr.task_id, tr.task, tr.scale_name, tr.category, tr.data_value, 
               tr.standard_error, tr.lower_ci_bound, tr.upper_ci_bound, tr.n as sample_size, 
               tr.date, tr.domain_source, tc.category_description
        FROM task_ratings tr
        LEFT JOIN task_categories tc 
          ON tr.scale_id = tc.scale_id AND tr.category = tc.category
        WHERE tr.o_net_soc_code = ? AND tr.category IS NOT NULL
        ORDER BY CAST(tr.data_value AS REAL) DESC
        LIMIT ?
        """,
        (code, n),
    ).fetchall()
    return _rows_to_dicts(rows)


def resolve_occupation(
    conn: sqlite3.Connection, query_or_code: str
) -> tuple[str | None, str]:
    """
    Resolve free text (e.g. "software developer") or an exact O*NET-SOC code
    (e.g. "15-1252.00") to a SOC code. Returns (code, "") on success, or
    (None, error_message) if nothing matched.
    """
    code = query_or_code.strip()
    looks_like_code = "-" in code and "." in code
    if not looks_like_code:
        results = _search_occupations(conn, code)
        if not results:
            return None, f"No occupations found matching '{query_or_code}'."
        code = str(results[0]["O*NET-SOC Code"])
    return code, ""


def section_report(
    conn: sqlite3.Connection,
    query_or_code: str,
    sections: list[str],
    fmt: str = "markdown",
) -> str:
    """
    Resolve query_or_code and return a report restricted to the given
    sections (pass [] for just the occupation summary + Job Zone, no
    detail sections). Shared by every granular per-section MCP tool so
    each one is a one-line wrapper around this function.
    """
    code, err = resolve_occupation(conn, query_or_code)
    if err:
        return err
    data = gather_occupation_data(conn, code, sections)
    if not data:
        return f"No data found for O*NET-SOC code: {code}"
    return format_json(data) if fmt == "json" else format_markdown(data)


def gather_occupation_data(
    conn: sqlite3.Connection, code: str, sections: list[str] | None = None
) -> dict[str, Any]:
    include_all = sections is None
    result: dict[str, Any] = {}

    occ = _get_by_code(conn, code)
    if not occ:
        return {}
    result["occupation"] = occ

    jz_row = conn.execute(
        "SELECT * FROM job_zones WHERE o_net_soc_code = ?", (code,)
    ).fetchone()
    if jz_row:
        jz_num = jz_row["job_zone"]
        result["job_zone"] = jz_num
        jz_ref = conn.execute(
            "SELECT * FROM job_zone_reference WHERE job_zone = ?", (jz_num,)
        ).fetchone()
        if jz_ref:
            result["job_zone_detail"] = dict(jz_ref)

    if include_all or "knowledge" in sections:
        result["knowledge"] = _top_rated(conn, "knowledge", code)

    if include_all or "skills" in sections:
        skill_items: list[dict[str, Any]] = []
        seen_elements: set[str] = set()
        for table in ("skills", "essential_skills", "transferable_skills"):
            if _pick_table(conn, table) is None:
                continue
            for item in _top_rated(conn, table, code):
                key = str(item.get("element_id") or item.get("element_name") or "")
                if key and key in seen_elements:
                    continue
                if key:
                    seen_elements.add(key)
                skill_items.append(item)
        skill_items.sort(key=lambda r: float(r.get("data_value") or 0), reverse=True)
        result["skills"] = skill_items[:15]

    if include_all or "abilities" in sections:
        result["abilities"] = _top_rated(conn, "abilities", code)

    if include_all or "activities" in sections:
        result["work_activities"] = _top_rated(conn, "work_activities", code)

    if include_all or "technology" in sections:
        table = _pick_table(conn, "software_skills", "technology_skills")
        if table:
            cols = _table_columns(conn, table)
            example_col = "workplace_example" if "workplace_example" in cols else "example"
            rows = conn.execute(
                f"""
                SELECT * FROM "{table}"
                WHERE o_net_soc_code = ?
                ORDER BY
                    CASE WHEN hot_technology = 'Y' THEN 0 ELSE 1 END,
                    "{example_col}"
                """,
                (code,),
            ).fetchall()
            result["technology_skills"] = _rows_to_dicts(rows)

    if include_all or "tasks" in sections:
        rows = conn.execute(
            """
            SELECT * FROM task_statements
            WHERE o_net_soc_code = ?
            ORDER BY
                CASE WHEN task_type = 'Core' THEN 0 ELSE 1 END,
                task
            """,
            (code,),
        ).fetchall()
        result["tasks"] = _rows_to_dicts(rows)

    if include_all or "education" in sections:
        edu_rows: list[dict[str, Any]] = []
        for table in (
            "education_training_and_experience",
            "education",
            "training_and_experience",
        ):
            if _pick_table(conn, table) is None:
                continue
            edu_rows.extend(
                _rows_to_dicts(
                    conn.execute(
                        f'SELECT * FROM "{table}" WHERE o_net_soc_code = ?',
                        (code,),
                    ).fetchall()
                )
            )
        result["education"] = edu_rows

        cats: list[dict[str, Any]] = []
        for table in (
            "education_training_and_experience_categories",
            "education_categories",
            "training_and_experience_categories",
        ):
            if _pick_table(conn, table) is None:
                continue
            cats.extend(_rows_to_dicts(conn.execute(f'SELECT * FROM "{table}"').fetchall()))
        result["education_categories"] = cats

    if include_all or "styles" in sections:
        rows = conn.execute(
            """
            SELECT * FROM work_styles
            WHERE o_net_soc_code = ?
            ORDER BY CAST(data_value AS REAL) DESC
            """,
            (code,),
        ).fetchall()
        result["work_styles"] = _rows_to_dicts(rows)

    if include_all or "values" in sections:
        if _pick_table(conn, "work_values"):
            rows = conn.execute(
                """
                SELECT * FROM work_values
                WHERE o_net_soc_code = ? AND scale_id = 'EX'
                ORDER BY CAST(data_value AS REAL) DESC
                """,
                (code,),
            ).fetchall()
            result["work_values"] = _rows_to_dicts(rows)

    if include_all or "context" in sections:
        rows = conn.execute(
            """
            SELECT * FROM work_context
            WHERE o_net_soc_code = ? AND scale_id = 'CX'
            ORDER BY CAST(data_value AS REAL) DESC
            LIMIT 20
            """,
            (code,),
        ).fetchall()
        result["work_context"] = _rows_to_dicts(rows)

    if include_all or "related" in sections:
        rows = conn.execute(
            """
            SELECT * FROM related_occupations
            WHERE o_net_soc_code = ?
            ORDER BY
                CASE relatedness_tier
                    WHEN 'Primary-Short' THEN 0
                    WHEN 'Primary-Long' THEN 1
                    WHEN 'Secondary-Short' THEN 2
                    WHEN 'Secondary-Long' THEN 3
                    ELSE 9
                END,
                CAST("index" AS INTEGER)
            """,
            (code,),
        ).fetchall()
        result["related_occupations"] = _rows_to_dicts(rows)

    if include_all or "titles" in sections:
        table = _pick_table(conn, "job_titles", "alternate_titles")
        if table:
            rows = conn.execute(
                f'SELECT * FROM "{table}" WHERE o_net_soc_code = ?',
                (code,),
            ).fetchall()
            result["alternate_titles"] = _rows_to_dicts(rows)

    if include_all or "emerging_tasks" in sections:
        result["emerging_tasks"] = _get_emerging_tasks(conn, code)

    if include_all or "interests" in sections:
        result["interests"] = _get_interests(conn, code)

    if include_all or "tools" in sections:
        result["tools_used"] = _get_tools_used(conn, code)

    if include_all or "task_ratings" in sections:
        result["task_ratings"] = _get_task_ratings(conn, code)

    return result


def _fmt_val(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def format_markdown(data: dict[str, Any]) -> str:
    lines: list[str] = []
    occ = data.get("occupation", {})
    title = occ.get("title") or occ.get("Title", "Unknown")
    code = occ.get("o_net_soc_code") or occ.get("O*NET-SOC Code", "")
    desc = occ.get("description") or occ.get("Description", "")

    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**O\\*NET-SOC Code:** `{code}`")
    lines.append("")
    if desc:
        lines.append(f"> {desc}")
        lines.append("")

    jz = data.get("job_zone")
    jz_detail = data.get("job_zone_detail", {})
    if jz:
        jz_name = jz_detail.get("name", f"Job Zone {jz}")
        lines.append(f"**Job Zone:** {jz} — {jz_name}")
        if jz_detail.get("education"):
            lines.append(f"  - **Education:** {jz_detail['education']}")
        if jz_detail.get("experience"):
            lines.append(f"  - **Experience:** {jz_detail['experience']}")
        if jz_detail.get("job_training"):
            lines.append(f"  - **Training:** {jz_detail['job_training']}")
        lines.append("")

    for section_key, section_title in [
        ("knowledge", "Knowledge"),
        ("skills", "Skills"),
        ("abilities", "Abilities"),
        ("work_activities", "Work Activities"),
    ]:
        items = data.get(section_key)
        if not items:
            continue
        lines.append(f"## {section_title}")
        lines.append("")
        lines.append("| Element | Importance |")
        lines.append("|---------|-----------|")
        for item in items:
            name = item.get("element_name", "")
            val = _fmt_val(item.get("data_value"))
            lines.append(f"| {name} | {val} |")
        lines.append("")

    tech = data.get("technology_skills")
    if tech:
        lines.append("## Technology Skills")
        lines.append("")
        lines.append("| Technology | Category | Hot | In Demand |")
        lines.append("|-----------|----------|-----|-----------|")
        for item in tech:
            example = item.get("example") or item.get("workplace_example") or ""
            commodity = item.get("commodity_title") or item.get("element_name") or ""
            hot = "Y" if str(item.get("hot_technology", "")).strip() == "Y" else ""
            demand = "Y" if str(item.get("in_demand", "")).strip() == "Y" else ""
            lines.append(f"| {example} | {commodity} | {hot} | {demand} |")
        lines.append("")

    tasks = data.get("tasks")
    if tasks:
        lines.append("## Tasks")
        lines.append("")
        for item in tasks:
            task_type = str(item.get("task_type", "")).strip()
            badge = f" *({task_type})*" if task_type else ""
            lines.append(f"- {item.get('task', '')}{badge}")
        lines.append("")

    edu = data.get("education")
    if edu:
        cats_list = data.get("education_categories", [])
        cats_lookup: dict[tuple[str, Any], str] = {}
        for c in cats_list:
            key = (str(c.get("element_id", "")), c.get("category"))
            cats_lookup[key] = str(c.get("category_description", ""))

        lines.append("## Education, Training & Experience")
        lines.append("")
        by_element: dict[str, list[dict]] = {}
        for row in edu:
            ename = str(row.get("element_name", ""))
            by_element.setdefault(ename, []).append(row)

        for ename, rows in by_element.items():
            lines.append(f"### {ename}")
            lines.append("")
            cat_rows = [r for r in rows if r.get("category") is not None]
            if cat_rows:
                cat_rows.sort(key=lambda r: int(r.get("category", 0) or 0))
                lines.append("| Category | Description | % |")
                lines.append("|----------|-------------|---|")
                for cr in cat_rows:
                    cat_num = cr.get("category")
                    elem_id = str(cr.get("element_id", ""))
                    cat_desc = cats_lookup.get((elem_id, cat_num), f"Category {cat_num}")
                    pct = _fmt_val(cr.get("data_value"))
                    if float(pct or 0) > 0:
                        lines.append(f"| {cat_num} | {cat_desc} | {pct} |")
                lines.append("")

    styles = data.get("work_styles")
    if styles:
        lines.append("## Work Styles")
        lines.append("")
        lines.append("| Style | Score |")
        lines.append("|-------|-------|")
        for item in styles:
            lines.append(
                f"| {item.get('element_name', '')} | {_fmt_val(item.get('data_value'))} |"
            )
        lines.append("")

    values = data.get("work_values")
    if values:
        lines.append("## Work Values")
        lines.append("")
        lines.append("| Value | Extent |")
        lines.append("|-------|--------|")
        for item in values:
            lines.append(
                f"| {item.get('element_name', '')} | {_fmt_val(item.get('data_value'))} |"
            )
        lines.append("")

    ctx = data.get("work_context")
    if ctx:
        lines.append("## Work Context (Top Conditions)")
        lines.append("")
        lines.append("| Context | Mean |")
        lines.append("|---------|------|")
        for item in ctx:
            lines.append(
                f"| {item.get('element_name', '')} | {_fmt_val(item.get('data_value'))} |"
            )
        lines.append("")

    related = data.get("related_occupations")
    if related:
        lines.append("## Related Occupations")
        lines.append("")
        lines.append("| Occupation | Code | Tier |")
        lines.append("|-----------|------|------|")
        for item in related:
            lines.append(
                f"| {item.get('related_title', '')} "
                f"| `{item.get('related_o_net_soc_code', '')}` "
                f"| {item.get('relatedness_tier', '')} |"
            )
        lines.append("")

    alt = data.get("alternate_titles")
    if alt:
        lines.append("## Alternate Titles")
        lines.append("")
        for item in alt:
            t = item.get("alternate_title") or item.get("job_title") or ""
            if t:
                lines.append(f"- {t}")
        lines.append("")

    emerging = data.get("emerging_tasks")
    if emerging:
        lines.append("## Emerging Tasks")
        lines.append("")
        lines.append("*New responsibilities being added to this occupation (as of 08/2025):*")
        lines.append("")
        for item in emerging:
            task = item.get("task", "")
            category = item.get("category", "")
            source = item.get("domain_source", "")
            if task:
                badge = f" *(Category: {category})*" if category else ""
                source_note = f" — {source}" if source else ""
                lines.append(f"- {task}{badge}{source_note}")
        lines.append("")

    interests = data.get("interests")
    if interests:
        lines.append("## Occupational Interests (RIASEC)")
        lines.append("")
        lines.append("*RIASEC personality-occupation alignment. Higher scores indicate stronger alignment.*")
        lines.append("")
        lines.append("| Interest | Score |")
        lines.append("|----------|-------|")
        for item in interests:
            name = item.get("element_name", "")
            val = _fmt_val(item.get("data_value"))
            lines.append(f"| {name} | {val} |")
        lines.append("")

    tools = data.get("tools_used")
    if tools:
        lines.append("## Tools & Equipment Used")
        lines.append("")
        lines.append("| Tool / Equipment | Category |")
        lines.append("|-----------------|----------|")
        for item in tools:
            tool = item.get("example", "")
            category = item.get("commodity_title", "")
            if tool:
                lines.append(f"| {tool} | {category} |")
        lines.append("")

    task_ratings = data.get("task_ratings")
    if task_ratings:
        lines.append("## Task Frequency & Importance (with Confidence Intervals)")
        lines.append("")
        lines.append("*Shows how frequently tasks are performed. Includes statistical confidence intervals (95% CI).*")
        lines.append("")
        lines.append(
            "| Task | Frequency | 95% CI (Lower–Upper) | Frequency Category | Sample Size |"
        )
        lines.append("|------|-----------|----------------------|-------------------|-------------|")
        for item in task_ratings:
            task = item.get("task", "")
            freq = _fmt_val(item.get("data_value"))
            lower = _fmt_val(item.get("lower_ci_bound"))
            upper = _fmt_val(item.get("upper_ci_bound"))
            sample = item.get("sample_size", "")
            category_desc = item.get("category_description", "")
            if task:
                lines.append(
                    f"| {task} | {freq} | {lower}–{upper} | {category_desc} | {sample} |"
                )
        lines.append("")

    # Read version from DB metadata
    version = ""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT value FROM _metadata WHERE key = 'version'").fetchone()
        if row:
            version = row[0]
        conn.close()
    except Exception:
        pass

    lines.append("---")
    version_str = f" {version}" if version else ""
    lines.append(
        f"*Data from O\\*NET{version_str} Database. "
        f"Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).*"
    )
    lines.append("")

    return "\n".join(lines)


def format_json(data: dict[str, Any]) -> str:
    def _clean(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(v) for v in obj]
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return obj

    return json.dumps(_clean(data), indent=2, ensure_ascii=False)


def format_list_markdown(results: list[dict[str, Any]]) -> str:
    lines = ["# Matching Occupations", ""]
    lines.append("| O\\*NET-SOC Code | Title | Description |")
    lines.append("|----------------|-------|-------------|")
    for r in results:
        code = r.get("O*NET-SOC Code", "")
        title = r.get("Title", "")
        desc = str(r.get("Description", ""))[:120]
        if len(str(r.get("Description", ""))) > 120:
            desc += "..."
        lines.append(f"| `{code}` | {title} | {desc} |")
    lines.append("")
    lines.append(f"*{len(results)} occupation(s) found.*")
    return "\n".join(lines)


def format_list_json(results: list[dict[str, Any]]) -> str:
    return json.dumps(results, indent=2, ensure_ascii=False)


def _write_output(content: str, filepath: str | None) -> None:
    if filepath:
        Path(filepath).write_text(content, encoding="utf-8")
        print(f"Output written to {filepath}", file=sys.stderr)
    else:
        print(content)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search the O*NET database and generate occupation reports.",
        epilog="Examples:\n"
        "  uv run scripts/onet_search.py \"software developer\"\n"
        "  uv run scripts/onet_search.py --code 15-1252.00 --format json\n"
        "  uv run scripts/onet_search.py --list \"engineer\"\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Keyword to search occupation titles and descriptions",
    )
    parser.add_argument(
        "--code",
        help="Look up by exact O*NET-SOC code (e.g. 15-1252.00)",
    )
    parser.add_argument(
        "--list",
        metavar="KEYWORD",
        dest="list_keyword",
        help="List all occupations matching a keyword (no full report)",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write output to a file instead of stdout",
    )
    parser.add_argument(
        "--sections",
        nargs="+",
        choices=SECTION_KEYS,
        help="Limit report to specific sections",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Number of top items to show per rated section (default: 15)",
    )
    parser.add_argument(
        "--db",
        help="Path to SQLite database (default: references/onet.db)",
    )

    args = parser.parse_args()

    global DB_PATH
    if args.db:
        DB_PATH = Path(args.db)

    conn = _connect()

    if args.list_keyword:
        results = _search_occupations(conn, args.list_keyword)
        if not results:
            print(f"No occupations found matching '{args.list_keyword}'", file=sys.stderr)
            sys.exit(1)
        output = (
            format_list_json(results)
            if args.format == "json"
            else format_list_markdown(results)
        )
        _write_output(output, args.output)
        conn.close()
        return

    code: str | None = args.code

    if not code and not args.query:
        parser.error("Provide a search query or --code or --list")

    if not code:
        results = _search_occupations(conn, args.query)
        if not results:
            print(f"No occupations found matching '{args.query}'", file=sys.stderr)
            sys.exit(1)
        if len(results) > 1:
            print(
                f"Found {len(results)} matching occupations. Using best match:",
                file=sys.stderr,
            )
            for r in results[:5]:
                print(
                    f"  {r['O*NET-SOC Code']}  {r['Title']}",
                    file=sys.stderr,
                )
            if len(results) > 5:
                print(
                    f"  ... and {len(results) - 5} more. Use --list to see all.",
                    file=sys.stderr,
                )
            print(file=sys.stderr)
        code = str(results[0]["O*NET-SOC Code"])

    data = gather_occupation_data(conn, code, args.sections)
    if not data:
        print(f"No data found for O*NET-SOC code: {code}", file=sys.stderr)
        sys.exit(1)

    if not _has_rated_data(conn, code):
        children = _find_child_codes(conn, code)
        if children:
            occ = data.get("occupation", {})
            title = occ.get("title", code)
            print(
                f"Note: {title} ({code}) is a parent category with no detailed "
                f"skills/knowledge/abilities data.",
                file=sys.stderr,
            )
            print(
                f"Detailed data is available on {len(children)} specialized occupation(s):",
                file=sys.stderr,
            )
            for child in children:
                print(f"  {child['code']}  {child['title']}", file=sys.stderr)
            print(
                f"\nRe-run with a specific code, e.g.:\n"
                f"  uv run scripts/onet_search.py --code {children[0]['code']}",
                file=sys.stderr,
            )
            print(file=sys.stderr)

    output = (
        format_json(data) if args.format == "json" else format_markdown(data)
    )
    _write_output(output, args.output)
    conn.close()


if __name__ == "__main__":
    main()

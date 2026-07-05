"""The agent: a LangGraph ReAct loop that acts ONLY through MCP tools.

Loads tools from the three MCP servers (Acquisition, Comprehension, Publishing)
via langchain-mcp-adapters, then runs a create_react_agent. The system prompt
encodes the agentic policy: plan acquisition from observation, self-correct on
validator contradictions, and decide when to escalate based on confidence.

The confidence gate + escalation is enforced in agent/run.py (post-hoc), so the
decision is auditable and not buried inside the LLM.
"""
import asyncio
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

AGENT_PROMPT = f"""You are MasjidOS, an agent that reads a masjid's prayer timetable
and prepares it for publishing. You act ONLY by calling tools.

IMPORTANT: the URL you are given is the masjid's WEBSITE, not necessarily the page
that shows the timetable. Do not assume the timetable is at that exact URL.

For the given masjid (name + url), follow this REASONING approach — do not assume a
fixed path, decide based on what each tool returns:

0. Call fetch_html on the given url first.
   - If has_tables is true and table_text looks like an actual prayer timetable
     (contains prayer names / time patterns), you may already be done acquiring —
     skip to step 2.
   - Otherwise, this is probably just the homepage. Call find_timetable_page on the
     same url. Look at the returned (text, href) pairs and REASON about which one
     most plausibly leads to a prayer/Salah/Iqamah timetable — masjids label this
     inconsistently ("Prayer Times", "Salah Timetable", "Namaz", "Athan", "Timings",
     "Iqamah", etc.), so match by MEANING, not a fixed keyword. Then call fetch_html
     on that chosen link.
   - If that page still has no usable table, decide: is there a pdf_link? call
     find_pdf_link then pdf_to_text. If pdf_to_text reports is_image_pdf true, call
     render_pdf_page_image instead.
   - If the timetable is an image on the page (check image_srcs), call
     render_image_from_url on the most likely one (look for filenames/alt text
     suggesting "prayer", "salah", "timetable", "timings").
   - If fetch_html (and find_timetable_page's chosen link) report no tables, no
     PDF links, and no plausible timetable image, the timetable may be injected
     client-side by a widget/plugin after page load. Call fetch_rendered_html on
     the same URL (it executes JavaScript with a headless browser) before giving
     up, then re-check its table_text/pdf_links/image_srcs the same way.
   - If you try 2-3 reasonable navigation attempts (including fetch_rendered_html)
     and still find nothing usable, stop and return an extraction with
     overall_confidence 0 and rationale explaining you could not locate the
     timetable — do not guess.
1. (folded into step 0 above.)
2. Call vlm_read_timetable with the FULL table_text/page_text you acquired — do
   not truncate or summarize it yourself before passing it on. The source is
   normally a whole month calendar; the tool needs every date row to extract all
   of them, not just one day.
2b. Jumuah (Friday prayer) times are often shown separately from the main daily
    timetable — sometimes only as a text banner/notice on the masjid's HOMEPAGE,
    not on the timetable page you acquired in step 0. If the returned extraction's
    column_confidence.iqamah["Jumuah 1"] or ["Jumuah 2"] is low and
    not_applicable.iqamah for that column is false (i.e. genuinely unknown, not
    proven absent), and you have not already fetched the site's bare homepage
    (scheme://domain/), call fetch_html (or fetch_rendered_html if that yields no
    tables/page_text) on the homepage and check its page_text for a Jumuah/Friday-
    prayer mention. If found, call vlm_recheck_field with that text as source_text
    and a contradiction describing the missing Jumuah field, to merge it in. Do
    this at most once — if still not found, leave it as low confidence rather
    than guessing.
3. Call validate_extraction on the returned extraction.
   - If it reports issues, call vlm_recheck_field with the specific contradiction to
     self-correct, then validate again. Do at most 2 correction rounds.
4. Return the FINAL extraction JSON as your answer. Do NOT generate CSVs or upload —
   that decision is made by the controller based on confidence.

Be economical with tool calls. If a tool errors, reason about an alternative rather
than repeating the same call.

CRITICAL OUTPUT FORMAT: your FINAL message must be ONLY the raw extraction JSON
object returned by validate_extraction/vlm_read_timetable — no markdown, no bullet
points, no prose summary, no code fences, no commentary before or after. A
downstream parser reads your final message with json.loads() and will crash on
anything else.
"""


async def build_agent():
    client = MultiServerMCPClient({
        "acquisition": {
            "command": sys.executable,
            "args": [str(ROOT / "mcp_servers" / "acquisition_server.py")],
            "transport": "stdio",
        },
        "comprehension": {
            "command": sys.executable,
            "args": [str(ROOT / "mcp_servers" / "comprehension_server.py")],
            "transport": "stdio",
        },
        "publishing": {
            "command": sys.executable,
            "args": [str(ROOT / "mcp_servers" / "publishing_server.py")],
            "transport": "stdio",
        },
    })
    tools = await client.get_tools()
    llm = ChatOpenAI(model=settings.agent_model, temperature=0,
                     api_key=settings.openai_api_key)
    agent = create_react_agent(llm, tools, prompt=AGENT_PROMPT)
    return agent, client


def _extract_trace(messages) -> list[dict]:
    """Turn the LangGraph message list into a displayable trace: which tool was
    called, with what arguments, and what it returned. This is what makes the
    agent's reasoning visible instead of a black box."""
    trace = []
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                trace.append({"type": "tool_call", "tool": tc.get("name"),
                             "args": tc.get("args")})
        elif m.__class__.__name__ == "ToolMessage":
            trace.append({"type": "tool_result", "tool": getattr(m, "name", "?"),
                         "content": str(getattr(m, "content", ""))[:1500]})
    return trace


async def comprehend(masjid_name: str, url: str) -> tuple[str, list[dict]]:
    """Run the agent for one masjid. Returns (final extraction JSON as string,
    the step-by-step tool-call trace)."""
    agent, _ = await build_agent()
    msg = f"Masjid name: {masjid_name}\nURL: {url}\n\nRead and map this timetable."
    result = await agent.ainvoke({"messages": [{"role": "user", "content": msg}]})
    final_text = result["messages"][-1].content
    trace = _extract_trace(result["messages"])
    return final_text, trace

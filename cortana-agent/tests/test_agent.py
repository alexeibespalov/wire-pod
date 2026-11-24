from langchain_core.messages import HumanMessage, ToolMessage

from app import agent, prompt, skills
from app.tools import TOOLS_BY_NAME


def test_core_tools_exist():
    assert set(agent.CORE_TOOLS) <= set(TOOLS_BY_NAME)


def test_skills_shipped():
    assert {"home", "timers", "skill-making"} <= set(skills.SKILLS)
    assert "home_*" in skills.SKILLS["home"]["tools"]


def test_home_skill_unlocks_home_tools():
    available = {**TOOLS_BY_NAME, "home_HassTurnOn": None, "home_todo__get_items": None, "tavily_search": None}
    msgs = [HumanMessage("turn on the lights")]
    names = agent.active_tool_names(msgs, available, ["tavily_search"])
    assert "home_HassTurnOn" not in names and "tavily_search" in names and "set_timer" in names
    msgs.append(ToolMessage(content="SKILL home loaded. Tools now available: home_*.", tool_call_id="1",
                            name="load_skill"))
    names = agent.active_tool_names(msgs, available, ["tavily_search"])
    assert {"home_HassTurnOn", "home_todo__get_items"} <= set(names)


def test_prompt_builds():
    p = prompt.build_system_prompt(["tavily_search"])
    assert "Cortana" in p and "tavily_search" in p and "- home:" in p
    assert "Vector" not in p and "robot" not in p.lower()
    assert prompt.build_context().startswith("[RIGHT NOW]\nIt is ")

from app.orchestration.engine import END, START, StateGraph
from app.orchestration.nodes import ConversationGraphNodes
from app.orchestration.states import NormalGraphState


def build_normal_graph(nodes: ConversationGraphNodes, *, checkpointer=None):
    """Bounded ReAct graph for the public ``normal`` mode.

    The graph understands the user goal once, optionally reuses a mature Recipe, and
    otherwise executes ready independent MCP reads in bounded parallel groups. The loop is bounded
    and duplicate calls are rejected, preventing expert-style over-reasoning on small
    questions while preserving dynamic planning for tasks outside the Recipe library.
    """

    graph = StateGraph(NormalGraphState)
    graph.add_node("load_context", nodes.load_context)
    graph.add_node("resolve_entity_context", nodes.resolve_entity_context)
    graph.add_node("activate_evidence_context", nodes.activate_evidence_context)
    graph.add_node("prepare_content", nodes.prepare_content)
    graph.add_node("react_decide", nodes.normal_react_decide)
    graph.add_node("react_execute", nodes.normal_react_execute)
    graph.add_node("wait_entity_selection", nodes.wait_for_entity_selection)
    graph.add_node("entity_resolution_failure", nodes.entity_resolution_failure)
    graph.add_node("finalize", nodes.normal_react_finalize)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "prepare_content")
    graph.add_edge("prepare_content", "resolve_entity_context")
    graph.add_conditional_edges(
        "resolve_entity_context",
        nodes.route_after_entity_resolution,
        {
            "selection": "wait_entity_selection",
            "failure": "entity_resolution_failure",
            "continue": "activate_evidence_context",
        },
    )
    graph.add_edge("activate_evidence_context", "react_decide")
    graph.add_conditional_edges(
        "react_decide",
        nodes.route_normal_react,
        {"execute": "react_execute", "finalize": "finalize", "confirmation": END},
    )
    graph.add_conditional_edges(
        "react_execute",
        nodes.route_after_agent_execution,
        {"selection": "wait_entity_selection", "failure": "entity_resolution_failure", "continue": "react_decide"},
    )
    graph.add_edge("wait_entity_selection", END)
    graph.add_edge("entity_resolution_failure", END)
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer) if checkpointer is not None else graph.compile()

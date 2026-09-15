from app.orchestration.engine import END, START, StateGraph
from app.orchestration.nodes import ConversationGraphNodes
from app.orchestration.states import QuickGraphState


def build_quick_graph(nodes: ConversationGraphNodes, *, checkpointer=None):
    graph = StateGraph(QuickGraphState)
    graph.add_node("load_context", nodes.load_context)
    graph.add_node("resolve_entity_context", nodes.resolve_entity_context)
    graph.add_node("activate_evidence_context", nodes.activate_evidence_context)
    graph.add_node("prepare_content", nodes.prepare_content)
    graph.add_node("quick_answer", nodes.quick_answer)
    graph.add_node("wait_entity_selection", nodes.wait_for_entity_selection)
    graph.add_node("entity_resolution_failure", nodes.entity_resolution_failure)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "resolve_entity_context")
    graph.add_conditional_edges(
        "resolve_entity_context",
        nodes.route_after_entity_resolution,
        {
            "selection": "wait_entity_selection",
            "failure": "entity_resolution_failure",
            "continue": "activate_evidence_context",
        },
    )
    graph.add_edge("activate_evidence_context", "prepare_content")
    graph.add_edge("prepare_content", "quick_answer")
    graph.add_edge("wait_entity_selection", END)
    graph.add_edge("entity_resolution_failure", END)
    graph.add_edge("quick_answer", END)
    return graph.compile(checkpointer=checkpointer) if checkpointer is not None else graph.compile()

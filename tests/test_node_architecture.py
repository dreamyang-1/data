from app.adapters import build_mock_adapters
from app.config import Settings
from app.graph import FRAMEWORK_NODE_NAMES, PRIMARY_NODE_NAMES, build_workflow
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


def _workflow():
    orchestrator = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )
    return build_workflow(orchestrator)


def test_graph_exposes_all_primary_and_framework_nodes():
    graph_nodes = set(_workflow().get_graph().nodes)
    assert set(PRIMARY_NODE_NAMES) <= graph_nodes
    assert set(FRAMEWORK_NODE_NAMES) <= graph_nodes


def test_primary_catalog_has_exactly_24_unique_nodes():
    assert len(PRIMARY_NODE_NAMES) == 24
    assert len(set(PRIMARY_NODE_NAMES)) == 24

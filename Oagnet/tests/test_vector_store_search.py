from vector_store import ChromaVectorStore


class CollectionWithoutCount:
    def __init__(self) -> None:
        self.kwargs = None

    def count(self):
        raise AssertionError("search must not issue a separate count query")

    def query(self, **kwargs):
        self.kwargs = kwargs
        return {
            "ids": [[]],
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }


def test_search_queries_chroma_once_and_accepts_empty_result() -> None:
    collection = CollectionWithoutCount()
    store = object.__new__(ChromaVectorStore)
    store._collection = collection

    results = store.search(
        [0.1, 0.2], top_k=8, where={"type": "entity"}
    )

    assert results == []
    assert collection.kwargs == {
        "query_embeddings": [[0.1, 0.2]],
        "n_results": 8,
        "where": {"type": "entity"},
    }

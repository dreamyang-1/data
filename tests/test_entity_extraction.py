import asyncio

import pytest
import httpx

from app.services.entity_extraction import (
    GLiNERCandidateExtractor,
    HttpEntityCandidateExtractor,
)


class FakeModel:
    def __init__(self, values):
        self.values = values

    def predict_entities(self, text, labels, threshold):
        return self.values


@pytest.mark.asyncio
async def test_gliner_candidates_must_be_exact_original_spans():
    text = "查询振德医疗品牌产品"
    extractor = GLiNERCandidateExtractor(model_name="unused", threshold=0.7)
    extractor._model = FakeModel([
        {"text": "振德医疗", "label": "品牌", "score": 0.95, "start": 2, "end": 6},
        {"text": "不存在", "label": "品牌", "score": 0.99, "start": 2, "end": 6},
        {"text": "产品", "label": "未知类型", "score": 0.99, "start": 8, "end": 10},
    ])
    values = await extractor.extract(text)
    assert [(item.text, item.label) for item in values] == [("振德医疗", "品牌")]


@pytest.mark.asyncio
async def test_gliner_model_loading_is_non_blocking_on_first_request():
    extractor = GLiNERCandidateExtractor(model_name="unused")

    async def delayed_model():
        await asyncio.sleep(10)

    extractor._load_model = delayed_model
    assert await extractor.extract("查询振德医疗") == []
    assert extractor._load_task is not None
    extractor._load_task.cancel()
    await asyncio.gather(extractor._load_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_extractor_validates_remote_spans_before_returning_them():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert payload["labels"]
        return httpx.Response(200, json={"entities": [
            {"text": "振德医疗", "label": "品牌", "score": 0.96, "start": 2, "end": 6},
            {"text": "伪造实体", "label": "品牌", "score": 0.99, "start": 2, "end": 6},
        ]})

    extractor = HttpEntityCandidateExtractor(
        url="http://extractor.local/entities",
        transport=httpx.MockTransport(handler),
    )
    result = await extractor.extract("查询振德医疗产品")
    assert [item.text for item in result] == ["振德医疗"]

from pathlib import Path

from app.domain.models import SkillConfig
from app.skills.dynamic import DynamicSkillLoader


def skill_file(root: Path, content: str) -> Path:
    path = root / "A_TEST" / "trend_helper" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_updated_skill_is_visible_without_restarting(tmp_path):
    path = skill_file(tmp_path, "---\nname: trend\n---\nversion one")
    loader = DynamicSkillLoader(tmp_path)
    config = SkillConfig(code="A_TEST", slug="trend_helper")

    first = loader.load(config)
    path.write_text("---\nname: trend\n---\nversion two with update", encoding="utf-8")
    second = loader.load(config)

    assert first is not None and second is not None
    assert first.sha256 != second.sha256
    assert "version two" in second.content
    assert second.stale is False


def test_invalid_update_uses_last_known_good_version(tmp_path):
    path = skill_file(tmp_path, "valid instructions")
    loader = DynamicSkillLoader(tmp_path, max_bytes=32)
    config = SkillConfig(code="A_TEST", slug="trend_helper")

    first = loader.load(config)
    path.write_text("x" * 100, encoding="utf-8")
    fallback = loader.load(config)

    assert first is not None and fallback is not None
    assert fallback.content == first.content
    assert fallback.sha256 == first.sha256
    assert fallback.stale is True


def test_missing_skill_does_not_expose_another_directory(tmp_path):
    loader = DynamicSkillLoader(tmp_path)
    assert loader.load(SkillConfig(code="A_TEST", slug="missing")) is None


def test_unicode_platform_skill_slug_is_supported(tmp_path):
    path = tmp_path / "A_TEST" / "趋势分析" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("趋势分析说明", encoding="utf-8")

    loaded = DynamicSkillLoader(tmp_path).load(
        SkillConfig(code="A_TEST", slug="趋势分析")
    )

    assert loaded is not None
    assert loaded.slug == "趋势分析"


def test_skill_slug_rejects_path_traversal():
    import pytest
    from pydantic import ValidationError

    for slug in ("..", "../secret", "folder/skill", "folder\\skill"):
        with pytest.raises(ValidationError):
            SkillConfig(code="A_TEST", slug=slug)


def test_mcp_routing_prefix_maps_to_portable_skill_directory(tmp_path):
    path = tmp_path / "A_TEST" / "diagnose" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("diagnostic instructions", encoding="utf-8")

    loaded = DynamicSkillLoader(tmp_path).load(
        SkillConfig(code="A_TEST", slug="mcp:diagnose")
    )

    assert loaded is not None
    assert loaded.slug == "mcp:diagnose"

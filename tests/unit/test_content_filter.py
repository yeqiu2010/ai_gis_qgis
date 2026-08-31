from __future__ import annotations

from ai_gis_qgis.backend.llm.content_filter import strip_hidden_reasoning


def test_strips_xml_thinking_block_and_keeps_clarifying_question():
    content = (
        "<think>需要分析很多内部步骤，但缺少字段名。\n继续推理。</think>\n\n"
        "请补充用于分类的字段名。"
    )

    assert strip_hidden_reasoning(content) == "请补充用于分类的字段名。"


def test_strips_multiple_reasoning_formats_without_removing_visible_text():
    content = (
        "<analysis>内部分析</analysis>\n"
        "```reasoning\n内部草稿\n```\n"
        "需要您选择目标图层。"
    )

    assert strip_hidden_reasoning(content) == "需要您选择目标图层。"


def test_keeps_normal_user_visible_content_unchanged():
    content = "请补充建筑类型字段；例如 `building_type`。"

    assert strip_hidden_reasoning(content) == content


def test_drops_unclosed_thinking_block_instead_of_leaking_it():
    content = "请先确认数据。\n<think>这里开始的是未闭合内部推理"

    assert strip_hidden_reasoning(content) == "请先确认数据。"

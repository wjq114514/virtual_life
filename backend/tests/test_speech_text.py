"""Deterministic segmentation and speech normalization."""

from virtual_life.speech_text import SpeechTextAssembler, normalize_spoken


def collect(text, *, steps=None, clock=None):
    assembler = SpeechTextAssembler(clock=clock)
    chunks = []
    if steps is None:
        chunks.extend(assembler.observe(text))
    else:
        for step in steps:
            chunks.extend(assembler.observe(step))
    chunks.extend(assembler.flush())
    return assembler, chunks


def test_cumulative_drafts_are_spoken_once():
    assembler, chunks = collect(
        "",
        steps=["你好", "你好，今天", "你好，今天天气", "你好，今天天气不错。"],
    )
    assert [chunk.spoken_text for chunk in chunks] == ["你好，今天天气不错。"]
    assert chunks[0].char_start == 0 and chunks[0].char_end == len("你好，今天天气不错。")


def test_long_reply_splits_without_losing_text():
    text = "第一句话已经结束。" * 8
    assembler, chunks = collect(text)
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(len(chunk.text) <= assembler.max_segment_chars for chunk in chunks)


def test_decimal_and_url_are_not_split_mid_token():
    text = "价格是 3.14 元，请访问 example.com 查看。"
    assembler, chunks = collect(text)
    joined = "".join(chunk.text for chunk in chunks)
    assert joined == text
    assert all("3." not in chunk.text[-1:] for chunk in chunks[:-1])


def test_soft_pause_ends_first_clause_after_minimum():
    assembler = SpeechTextAssembler(first_min_chars=6)
    chunks = assembler.observe("今天天气很好，阳光明媚，我们一起去公园里散步吧，然后再回来")
    assert chunks and chunks[0].text.endswith("，")
    assert len(chunks[0].text) >= 6


def test_pending_cap_forces_a_safe_break():
    clock = iter([0.0, 0.5])
    assembler = SpeechTextAssembler(
        first_min_chars=4, pending_cap_seconds=0.5, clock=lambda: next(clock)
    )
    pending = "一个没有标点的很长句子继续延伸"
    assert assembler.observe(pending) == []
    chunks = assembler.observe(pending)
    assert chunks and chunks[0].text == pending
    assert chunks[0].char_end == len(assembler.observed_text)


def test_provider_revision_is_flagged_and_not_spoken():
    assembler = SpeechTextAssembler()
    assembler.observe("你好，今天天气不错。")
    chunks = assembler.observe("你好，今天天气很糟糕。")
    assert chunks == []
    assert assembler.revision_anomaly == "spoken_prefix_changed"


def test_normalize_strips_markdown_and_links():
    assert (
        normalize_spoken("**你好**，见 [文档](https://a.b/c) 和 `code`") == "你好，见 文档 和 code"
    )
    assert normalize_spoken("```python\nprint(1)\n```") == "代码已显示在屏幕上"
    assert normalize_spoken("<b>加粗</b>文字") == "加粗文字"


def test_short_unpunctuated_draft_waits_without_zero_progress_loop():
    assembler = SpeechTextAssembler()
    assert assembler.observe("你好") == []
    assert assembler.pending_text == "你好"
    assert [chunk.text for chunk in assembler.flush()] == ["你好"]


def test_only_pending_text_can_be_revised():
    assembler = SpeechTextAssembler()
    assert assembler.observe("我想去") == []
    assert assembler.observe("我想看电影") == []
    assert assembler.revision_anomaly is None
    assert [chunk.text for chunk in assembler.flush()] == ["我想看电影"]
    assert assembler.observe("我想读书") == []
    assert assembler.revision_anomaly == "spoken_prefix_changed"


def test_pending_revision_preserves_the_already_emitted_prefix():
    assembler = SpeechTextAssembler()
    chunks = assembler.observe("第一句话。然后我想去")
    assert chunks and chunks[0].text == "第一句话。"
    assembler.observe("第一句话。然后我想看电影")
    assert assembler.revision_anomaly is None
    assert "".join(chunk.text for chunk in chunks + assembler.flush()) == "第一句话。然后我想看电影"


def test_english_sentence_ends_without_splitting_a_decimal():
    _, chunks = collect("It costs 3.14 dollars. Next sentence.")
    assert len(chunks) == 2
    assert chunks[0].text == "It costs 3.14 dollars."

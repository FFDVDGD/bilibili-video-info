import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_SPEC = spec_from_file_location(
    "bilibili_video_info_test_package",
    _PLUGIN_DIR / "plugin.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

BilibiliVideoInfoPlugin = _MODULE.BilibiliVideoInfoPlugin
VideoMetadata = _MODULE.VideoMetadata
_sample_transcript = _MODULE._sample_transcript
_sanitize_summary = _MODULE._sanitize_summary


def _video_metadata() -> VideoMetadata:
    return VideoMetadata(
        source_url="https://www.bilibili.com/video/BV1test",
        webpage_url="https://www.bilibili.com/video/BV1test",
        video_key="BV1test:p1",
        title="测试视频",
        description="用于测试的简介",
        uploader="测试作者",
        published_at="2026-07-31 12:00:00",
        duration_seconds=120,
        thumbnail_url="https://image.example/cover.jpg",
        subtitle_track=None,
    )


def test_summary_is_plain_text() -> None:
    assert _sanitize_summary("## 总结\n- **第一点**\n- 第二点", 300) == "总结\n第一点\n第二点"


def test_summary_is_limited_to_configured_characters() -> None:
    summary = _sanitize_summary("这是第一句。" + "后续内容" * 100, 20)
    assert len(summary) <= 20
    assert summary.endswith("…")


def test_long_transcript_sample_covers_start_middle_and_end() -> None:
    transcript = "A" * 4000 + "B" * 4000 + "C" * 4000
    sample = _sample_transcript(transcript, 3000)

    assert len(sample) < 3100
    assert "[开头]\n" + "A" * 100 in sample
    assert "[中段]\n" + "B" * 100 in sample
    assert "[结尾]\n" + "C" * 100 in sample


@pytest.mark.asyncio
async def test_transcript_classifier_prompt_covers_music_and_asr_noise() -> None:
    plugin = BilibiliVideoInfoPlugin()
    prompts: list[str] = []

    async def fake_call(prompt: str, *, max_tokens: int) -> str:
        prompts.append(prompt)
        assert max_tokens == 20
        return "MUSIC_ONLY"

    plugin._call_llm = fake_call
    verdict = await plugin._classify_transcript(
        _video_metadata(),
        "啦啦啦……（音乐）啦啦啦……",
        "Fun-ASR语音转写",
    )

    assert verdict == "MUSIC_ONLY"
    assert "纯音乐/BGM" in prompts[0]
    assert "明显是无意义的 ASR 幻觉" in prompts[0]
    assert "标题和简介仅用于辅助理解，不能代替抽取文本" in prompts[0]


@pytest.mark.asyncio
async def test_music_only_transcript_uses_natural_metadata_summary() -> None:
    plugin = BilibiliVideoInfoPlugin()
    plugin.set_plugin_config({})
    prompts: list[str] = []
    bad_transcript = "不要用于最终总结的音乐幻觉文本"

    async def fake_call(prompt: str, *, max_tokens: int) -> str:
        prompts.append(prompt)
        if max_tokens == 20:
            return "MUSIC_ONLY"
        assert max_tokens == 600
        return "视频围绕测试主题展开，简介展示了用于测试的基本信息。"

    plugin._call_llm = fake_call
    summary = await plugin._summarize(_video_metadata(), bad_transcript, "Fun-ASR语音转写")

    assert summary == "视频围绕测试主题展开，简介展示了用于测试的基本信息。"
    assert bad_transcript not in prompts[-1]
    assert "内部资料来源（禁止在总结中提及）：标题和简介" in prompts[-1]
    assert "内部语音筛选结果（禁止在总结中提及）：纯音乐/BGM" in prompts[-1]
    assert "不要出现“转写质量不足”" in prompts[-1]
    assert "不要输出 USEFUL 等内部标签、筛选结果或判断过程" in prompts[-1]


@pytest.mark.asyncio
async def test_missing_transcript_hides_processing_failure_from_summary() -> None:
    plugin = BilibiliVideoInfoPlugin()
    plugin.set_plugin_config({})
    prompts: list[str] = []

    async def fake_call(prompt: str, *, max_tokens: int) -> str:
        prompts.append(prompt)
        assert max_tokens == 600
        return "视频介绍了测试主题及其基本背景。"

    plugin._call_llm = fake_call
    summary = await plugin._summarize(
        _video_metadata(),
        "",
        "仅标题和简介（字幕及语音转写不可用）",
    )

    assert summary == "视频介绍了测试主题及其基本背景。"
    assert "字幕及语音转写不可用" not in prompts[0]
    assert "内部资料来源（禁止在总结中提及）：标题和简介" in prompts[0]
    assert "不要说明资料不足、数据来源或技术处理过程" in prompts[0]


@pytest.mark.asyncio
async def test_long_useful_transcript_keeps_chunk_validity_for_final_review() -> None:
    plugin = BilibiliVideoInfoPlugin()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "0.1.0"},
            "summary": {"transcript_chunk_chars": 2000},
        }
    )
    prompts: list[tuple[str, int]] = []

    async def fake_call(prompt: str, *, max_tokens: int) -> str:
        prompts.append((prompt, max_tokens))
        if max_tokens == 20:
            return "USEFUL"
        if max_tokens == 300:
            return "片段有效性：有效\n视频在解释一个可验证的测试结论。"
        return "视频解释了一个可验证的测试结论。"

    plugin._call_llm = fake_call
    transcript = "这是一段连贯且有信息量的讲解。" * 150
    summary = await plugin._summarize(_video_metadata(), transcript, "Fun-ASR语音转写")

    chunk_prompts = [prompt for prompt, max_tokens in prompts if max_tokens == 300]
    final_prompt = [prompt for prompt, max_tokens in prompts if max_tokens == 600][0]
    assert summary == "视频解释了一个可验证的测试结论。"
    assert len(chunk_prompts) > 1
    assert all("片段有效性：有效/部分有效/无效" in prompt for prompt in chunk_prompts)
    assert "片段有效性：有效" in final_prompt
    assert "必须独立判断字幕或转写中是否真的存在" in final_prompt


def test_build_job_accepts_any_group_and_preserves_message_id() -> None:
    plugin = BilibiliVideoInfoPlugin()
    job = plugin._build_job(
        {
            "platform": "qq",
            "session_id": "stream-1",
            "message_id": "message-1",
            "processed_plain_text": "看看 https://b23.tv/AbCdEf",
            "message_info": {
                "group_info": {"group_id": "123"},
                "user_info": {"user_nickname": "测试用户"},
                "additional_config": {"account_id": "bot-1"},
            },
        }
    )

    assert job is not None
    assert job.group_id == "123"
    assert job.message_id == "message-1"
    assert job.account_id == "bot-1"


def test_build_job_extracts_url_from_bilibili_miniapp_card() -> None:
    plugin = BilibiliVideoInfoPlugin()
    job = plugin._build_job(
        {
            "platform": "qq",
            "session_id": "stream-1",
            "message_id": "message-1",
            "processed_plain_text": "[小程序] 哔哩哔哩：测试视频",
            "message_info": {
                "group_info": {"group_id": "123"},
                "user_info": {"user_nickname": "测试用户"},
                "additional_config": {
                    "self_id": "bot-1",
                    "platform_card_payloads": [
                        {
                            "type": "miniapp_card",
                            "app": "com.tencent.miniapp_01",
                            "payload": {
                                "app": "com.tencent.miniapp_01",
                                "meta": {
                                    "detail_1": {
                                        "title": "哔哩哔哩",
                                        "desc": "测试视频",
                                        "qqdocurl": "https://b23.tv/AbCdEf",
                                    }
                                },
                            },
                        }
                    ],
                },
            },
        }
    )

    assert job is not None
    assert job.url == "https://b23.tv/AbCdEf"
    assert job.text == "[小程序] 哔哩哔哩：测试视频"


def test_build_job_ignores_private_chat() -> None:
    plugin = BilibiliVideoInfoPlugin()
    job = plugin._build_job(
        {
            "platform": "qq",
            "session_id": "stream-1",
            "processed_plain_text": "看看 https://b23.tv/AbCdEf",
            "message_info": {"group_info": None, "user_info": {"user_nickname": "测试用户"}},
        }
    )

    assert job is None

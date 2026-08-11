import logging
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

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
InboundVideoJob = _MODULE.InboundVideoJob
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


def _video_job() -> InboundVideoJob:
    return InboundVideoJob(
        url="https://www.bilibili.com/video/BV1test",
        text="看看这个视频",
        platform="qq",
        group_id="123",
        user_nickname="测试用户",
        stream_id="stream-1",
        message_id="message-1",
        account_id="bot-1",
        scope="",
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
@pytest.mark.parametrize("verdict", ["MUSIC_ONLY", "NO_MEANING"])
async def test_invalid_transcript_skips_final_summary(verdict: str) -> None:
    plugin = BilibiliVideoInfoPlugin()
    plugin.set_plugin_config({})
    calls: list[int] = []

    async def fake_call(prompt: str, *, max_tokens: int) -> str:
        del prompt
        calls.append(max_tokens)
        if max_tokens == 20:
            return verdict
        pytest.fail("无效字幕不应调用最终总结模型")

    plugin._call_llm = fake_call
    summary = await plugin._summarize(_video_metadata(), "无效字幕文本", "Fun-ASR语音转写")

    assert summary is None
    assert calls == [20]


@pytest.mark.asyncio
async def test_missing_transcript_skips_all_llm_calls() -> None:
    plugin = BilibiliVideoInfoPlugin()
    plugin.set_plugin_config({})

    async def fake_call(prompt: str, *, max_tokens: int) -> str:
        del prompt, max_tokens
        pytest.fail("没有字幕或转写时不应调用 LLM")

    plugin._call_llm = fake_call
    summary = await plugin._summarize(
        _video_metadata(),
        "",
        "仅标题和简介（字幕及语音转写不可用）",
    )

    assert summary is None


@pytest.mark.asyncio
async def test_classifier_failure_skips_final_summary() -> None:
    plugin = BilibiliVideoInfoPlugin()
    plugin.set_plugin_config({})
    plugin._set_context(SimpleNamespace(logger=logging.getLogger("test-classifier-failure")))
    calls: list[int] = []

    async def fake_call(prompt: str, *, max_tokens: int) -> str:
        del prompt
        calls.append(max_tokens)
        raise RuntimeError("classifier unavailable")

    plugin._call_llm = fake_call
    summary = await plugin._summarize(_video_metadata(), "待判断的字幕", "中文字幕")

    assert summary is None
    assert calls == [20]


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
    assert "前置判断已确认字幕或转写中至少存在部分有效内容" in final_prompt
    assert "不能代替字幕或转写生成概览" in final_prompt


@pytest.mark.asyncio
async def test_summary_status_is_sent_and_retracted_through_napcat_api() -> None:
    plugin = BilibiliVideoInfoPlugin()
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeApi:
        async def call(self, api_name: str, **kwargs: object) -> dict[str, object]:
            calls.append((api_name, kwargs))
            if api_name == "adapter.napcat.group.send_group_msg":
                return {"status": "ok", "data": {"message_id": 456}}
            return {"status": "ok", "retcode": 0, "data": {}}

    plugin._set_context(
        SimpleNamespace(
            api=FakeApi(),
            logger=logging.getLogger("test-summary-status"),
        )
    )

    message_id = await plugin._send_summary_status(_video_job())
    await plugin._retract_summary_status(message_id)

    assert message_id == "456"
    assert calls == [
        (
            "adapter.napcat.group.send_group_msg",
            {
                "version": "1",
                "params": {
                    "group_id": 123,
                    "message": [{"type": "text", "data": {"text": "AI总结中"}}],
                },
            },
        ),
        (
            "adapter.napcat.message.delete_msg",
            {"version": "1", "message_id": "456"},
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transcript", "summary_result", "expected_text", "expected_summary_calls"),
    [
        ("", None, "未能获取有效字幕或语音转写，已跳过 AI 总结。", 0),
        ("无效字幕", None, "字幕或语音转写中没有可用于总结的有效内容，已跳过 AI 总结。", 1),
        ("有效字幕", "最终总结", "最终总结", 1),
    ],
)
async def test_workflow_retracts_status_after_summary_result(
    tmp_path: Path,
    transcript: str,
    summary_result: str | None,
    expected_text: str,
    expected_summary_calls: int,
) -> None:
    plugin = BilibiliVideoInfoPlugin()
    plugin.set_plugin_config({})
    plugin._http_client = object()
    events: list[tuple[str, str]] = []
    summary_calls = 0

    class FakeYtDlp:
        async def probe(self, url: str, work_dir: Path, cookie_file: Path | None) -> VideoMetadata:
            del url, work_dir, cookie_file
            return _video_metadata()

    async def fake_send_metadata(metadata: VideoMetadata, stream_id: str) -> None:
        del metadata, stream_id

    async def fake_send_status(job: InboundVideoJob) -> str:
        del job
        events.append(("status", "456"))
        return "456"

    async def fake_obtain_transcript(
        metadata: VideoMetadata,
        work_dir: Path,
        cookie_file: Path | None,
    ) -> tuple[str, str]:
        del metadata, work_dir, cookie_file
        return transcript, "中文字幕"

    async def fake_summarize(metadata: VideoMetadata, text: str, source_label: str) -> str | None:
        nonlocal summary_calls
        del metadata, text, source_label
        summary_calls += 1
        return summary_result

    async def fake_send_text(text: str, stream_id: str) -> None:
        del stream_id
        events.append(("result", text))

    async def fake_retract(message_id: str | None) -> None:
        events.append(("retract", str(message_id)))

    plugin._yt_dlp = FakeYtDlp()
    plugin._send_metadata = fake_send_metadata
    plugin._send_summary_status = fake_send_status
    plugin._obtain_transcript = fake_obtain_transcript
    plugin._summarize = fake_summarize
    plugin._send_context_text = fake_send_text
    plugin._retract_summary_status = fake_retract
    plugin._set_context(
        SimpleNamespace(
            paths=SimpleNamespace(runtime_dir=tmp_path),
            logger=logging.getLogger("test-summary-workflow"),
        )
    )

    await plugin._run_video_workflow(_video_job(), "stream-1")

    assert summary_calls == expected_summary_calls
    assert events[-2:] == [("result", expected_text), ("retract", "456")]


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


def test_build_job_ignores_url_from_quoted_message() -> None:
    plugin = BilibiliVideoInfoPlugin()
    job = plugin._build_job(
        {
            "platform": "qq",
            "session_id": "stream-1",
            "message_id": "message-1",
            "processed_plain_text": "https://b23.tv/QuotedVideo 这个视频说得对吗？",
            "raw_message": [
                {
                    "type": "reply",
                    "data": {
                        "target_message_id": "quoted-message",
                        "target_message_content": "https://b23.tv/QuotedVideo",
                    },
                },
                {"type": "text", "data": "这个视频说得对吗？"},
            ],
            "message_info": {
                "group_info": {"group_id": "123"},
                "user_info": {"user_nickname": "测试用户"},
            },
        }
    )

    assert job is None


def test_build_job_accepts_url_from_reply_body() -> None:
    plugin = BilibiliVideoInfoPlugin()
    job = plugin._build_job(
        {
            "platform": "qq",
            "session_id": "stream-1",
            "message_id": "message-1",
            "processed_plain_text": "https://b23.tv/QuotedVideo 看这个 https://b23.tv/CurrentVideo",
            "raw_message": [
                {
                    "type": "reply",
                    "data": {
                        "target_message_id": "quoted-message",
                        "target_message_content": "https://b23.tv/QuotedVideo",
                    },
                },
                {"type": "text", "data": "看这个 https://b23.tv/CurrentVideo"},
            ],
            "message_info": {
                "group_info": {"group_id": "123"},
                "user_info": {"user_nickname": "测试用户"},
            },
        }
    )

    assert job is not None
    assert job.url == "https://b23.tv/CurrentVideo"
    assert job.text == "看这个 https://b23.tv/CurrentVideo"


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

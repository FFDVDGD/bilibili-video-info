import json
from pathlib import Path

import httpx
import pytest

from bilibili import (
    BilibiliProcessingError,
    ToolPaths,
    YtDlpClient,
    _download_bilibili_resource,
    _normalize_bilibili_resource_url,
    extract_bilibili_url,
    format_duration,
    parse_subtitle,
    select_subtitle_track,
    write_netscape_cookie_file,
)


def test_extracts_supported_video_links() -> None:
    assert extract_bilibili_url("分享 https://www.bilibili.com/video/BV1xx411c7mD?p=2。") == (
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    )
    assert extract_bilibili_url("看看 https://b23.tv/AbCdEf") == "https://b23.tv/AbCdEf"
    assert extract_bilibili_url("番剧 https://www.bilibili.com/bangumi/play/ep12345") is not None


def test_ignores_non_video_bilibili_page() -> None:
    assert extract_bilibili_url("主页 https://space.bilibili.com/12345") is None


def test_cookie_header_is_converted_without_leaking_fields(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    write_netscape_cookie_file("SESSDATA=value%2Cmore; bili_jct=csrf; buvid3=id", cookie_file)
    content = cookie_file.read_text(encoding="utf-8")
    assert ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tvalue%2Cmore" in content
    assert "bili_jct\tcsrf" in content


def test_bilibili_json_subtitle_is_plain_text() -> None:
    payload = {"body": [{"content": "第一句"}, {"content": "第一句"}, {"content": "第二句"}]}
    assert parse_subtitle(json.dumps(payload).encode(), "json") == "第一句\n第二句"


def test_srt_subtitle_strips_timestamps_and_tags() -> None:
    content = b"1\n00:00:01,000 --> 00:00:02,000\n<b>Hello</b>\n\n2\n00:00:03,000 --> 00:00:04,000\nWorld\n"
    assert parse_subtitle(content, "srt") == "Hello\nWorld"


def test_other_manual_subtitle_is_preferred_to_chinese_ai_caption() -> None:
    info = {
        "subtitles": {
            "en": [{"url": "https://example/en.vtt", "ext": "vtt"}],
            "ai-zh": [{"url": "https://example/ai-zh.json", "ext": "json"}],
        },
        "automatic_captions": {
            "zh-CN": [{"url": "https://example/zh.json", "ext": "json"}],
        },
    }
    track = select_subtitle_track(info)
    assert track is not None
    assert track.language == "en"


def test_automatic_captions_are_ignored_for_asr_fallback() -> None:
    info = {
        "subtitles": {
            "ai-zh": [{"url": "https://example/ai-zh.json", "ext": "json"}],
        },
        "automatic_captions": {
            "zh-CN": [{"url": "https://example/zh.json", "ext": "json"}],
        },
    }

    assert select_subtitle_track(info) is None


async def test_probe_requests_and_reads_inline_manual_subtitle_data(tmp_path: Path) -> None:
    client = YtDlpClient(ToolPaths(yt_dlp=tmp_path / "yt-dlp", ffmpeg=tmp_path / "ffmpeg"))
    captured_arguments: list[str] = []

    async def fake_run(arguments: list[str], *, work_dir: Path, timeout_seconds: int) -> str:
        del work_dir, timeout_seconds
        captured_arguments.extend(arguments)
        return json.dumps(
            {
                "id": "BV1test",
                "title": "测试视频",
                "webpage_url": "https://www.bilibili.com/video/BV1test",
                "subtitles": {
                    "zh-CN": [
                        {
                            "ext": "srt",
                            "data": "1\n00:00:01,000 --> 00:00:02,000\n人工字幕\n",
                        }
                    ],
                    "ai-zh": [{"ext": "srt", "data": "AI 字幕"}],
                },
            }
        )

    client._run = fake_run  # type: ignore[method-assign]
    metadata = await client.probe("https://www.bilibili.com/video/BV1test", tmp_path, None)

    assert "--write-subs" in captured_arguments
    assert metadata.subtitle_track is not None
    assert metadata.subtitle_track.language == "zh-CN"
    assert metadata.subtitle_track.data.endswith("人工字幕\n")


def test_format_duration() -> None:
    assert format_duration(65) == "1:05"
    assert format_duration(3661) == "1:01:01"


def test_resource_url_only_accepts_official_https_hosts() -> None:
    assert _normalize_bilibili_resource_url("http://i0.hdslb.com/a.jpg") == "https://i0.hdslb.com/a.jpg"
    with pytest.raises(BilibiliProcessingError):
        _normalize_bilibili_resource_url("https://example.com/subtitle.json")


async def test_cookie_is_not_forwarded_to_cdn_redirect() -> None:
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        if request.url.host == "api.bilibili.com":
            return httpx.Response(302, headers={"location": "https://aisubtitle.hdslb.com/subtitle.json"})
        return httpx.Response(200, content=b"{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await _download_bilibili_resource(
            client,
            "https://api.bilibili.com/subtitle",
            headers={},
            cookie_header="SESSDATA=secret",
            timeout_seconds=5,
        )

    assert response.status_code == 200
    assert seen_headers[0]["cookie"] == "SESSDATA=secret"
    assert "cookie" not in seen_headers[1]

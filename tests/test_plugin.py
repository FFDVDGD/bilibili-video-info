import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

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
_sanitize_summary = _MODULE._sanitize_summary


def test_summary_is_plain_text() -> None:
    assert _sanitize_summary("## 总结\n- **第一点**\n- 第二点", 300) == "总结\n第一点\n第二点"


def test_summary_is_limited_to_configured_characters() -> None:
    summary = _sanitize_summary("这是第一句。" + "后续内容" * 100, 20)
    assert len(summary) <= 20
    assert summary.endswith("…")


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

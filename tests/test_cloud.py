import json
from pathlib import Path

import httpx
import pytest

import cloud
from cloud import (
    AsrSettings,
    CloudProcessingError,
    FunAsrClient,
    OssAudioStore,
    OssSettings,
    _extract_task_failure_detail,
    _extract_transcript,
    _safe_cloud_detail,
)


def test_extract_transcript_prefers_top_level_text() -> None:
    payload = {
        "transcripts": [
            {
                "text": "完整文本",
                "sentences": [{"text": "不应重复"}],
            }
        ]
    }
    assert _extract_transcript(payload) == "完整文本"


def test_extract_transcript_joins_sentences() -> None:
    payload = {"transcripts": [{"sentences": [{"text": "第一句"}, {"text": "第二句"}]}]}
    assert _extract_transcript(payload) == "第一句\n第二句"


async def test_fun_asr_transcription_flow_uses_stable_model_and_auto_language() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task-1", "task_status": "PENDING"}})
        if request.url.path.endswith("/tasks/task-1"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://result.example/transcript.json?token=secret",
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200, json={"transcripts": [{"text": "识别结果"}]})

    settings = AsrSettings(
        region="cn-beijing",
        workspace_id="workspace-1",
        api_key="sk-test",
        timeout_seconds=5,
        poll_interval_seconds=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await FunAsrClient(http_client, settings).transcribe("https://audio.example/file.mp3?signature=secret")

    assert result == "识别结果"
    body = json.loads(requests[0].content)
    assert body == {
        "model": "fun-asr",
        "input": {"file_urls": ["https://audio.example/file.mp3?signature=secret"]},
        "parameters": {"channel_id": [0]},
    }
    assert requests[0].headers["x-dashscope-async"] == "enable"
    assert requests[0].headers["x-dashscope-ossresourceresolve"] == "enable"
    assert requests[1].headers["authorization"] == "Bearer sk-test"
    assert "x-dashscope-async" not in requests[1].headers
    assert "x-dashscope-ossresourceresolve" not in requests[1].headers
    assert "authorization" not in requests[2].headers


async def test_fun_asr_retries_transient_internal_task_error(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    submit_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_count
        requests.append(request)
        if request.method == "POST":
            submit_count += 1
            return httpx.Response(200, json={"output": {"task_id": f"task-{submit_count}"}})
        if request.url.path.endswith("/tasks/task-1"):
            return httpx.Response(
                200,
                json={
                    "request_id": "request-1",
                    "output": {
                        "task_status": "FAILED",
                        "results": [
                            {
                                "subtask_status": "FAILED",
                                "code": "InternalError",
                                "message": "Internal server error!",
                            }
                        ],
                    },
                },
            )
        if request.url.path.endswith("/tasks/task-2"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://result.example/retried.json",
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200, json={"transcripts": [{"text": "重试成功"}]})

    monkeypatch.setattr(cloud, "_ASR_RETRY_DELAY_SECONDS", 0)
    settings = AsrSettings(
        region="cn-beijing",
        workspace_id="workspace-1",
        api_key="sk-test",
        timeout_seconds=5,
        poll_interval_seconds=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await FunAsrClient(http_client, settings).transcribe("https://audio.example/file.mp3")

    assert result == "重试成功"
    assert sum(request.method == "POST" for request in requests) == 2


async def test_fun_asr_failure_uses_subtask_error_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        return httpx.Response(
            200,
            json={
                "request_id": "request-1",
                "output": {
                    "task_status": "FAILED",
                    "message": "Internal server error!",
                    "results": [
                        {
                            "subtask_status": "FAILED",
                            "code": "InvalidFile.DownloadFailed",
                            "message": "The audio file cannot be downloaded.",
                        }
                    ],
                },
            },
        )

    settings = AsrSettings(
        region="cn-beijing",
        workspace_id="workspace-1",
        api_key="sk-test",
        timeout_seconds=5,
        poll_interval_seconds=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        error_pattern = r"InvalidFile\.DownloadFailed.*task_id=task-1.*request_id=request-1"
        with pytest.raises(CloudProcessingError, match=error_pattern):
            await FunAsrClient(http_client, settings).transcribe("https://audio.example/file.mp3")


async def test_oss_signed_url_preserves_object_path_slashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sign_url_kwargs: dict[str, object] = {}

    class FakeBucket:
        def put_object_from_file(self, object_key: str, audio_path: str) -> None:
            assert object_key.startswith("prefix/")
            assert audio_path.endswith("audio.mp3")

        def sign_url(self, method: str, object_key: str, expires: int, **kwargs: object) -> str:
            del method, object_key, expires
            sign_url_kwargs.update(kwargs)
            return "https://bucket.example/prefix/audio.mp3?signature=secret"

        def delete_object(self, object_key: str) -> None:
            del object_key

    monkeypatch.setattr(cloud.oss2, "Auth", lambda access_key_id, access_key_secret: object())
    monkeypatch.setattr(cloud.oss2, "Bucket", lambda auth, endpoint, bucket: FakeBucket())
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    store = OssAudioStore(
        OssSettings(
            endpoint="https://oss-cn-beijing.aliyuncs.com",
            bucket="bucket",
            access_key_id="id",
            access_key_secret="secret",
            object_prefix="prefix",
            signed_url_ttl_seconds=7200,
        )
    )

    uploaded = await store.upload(audio_path)

    assert uploaded.signed_url.startswith("https://bucket.example/")
    assert sign_url_kwargs == {"slash_safe": True}


def test_task_failure_detail_prefers_result_code_and_message() -> None:
    output = {
        "message": "Internal server error!",
        "results": [
            {
                "code": "InvalidFile.DownloadFailed",
                "message": "The audio file cannot be downloaded.",
            }
        ],
    }
    assert _extract_task_failure_detail(output, "FAILED") == (
        "InvalidFile.DownloadFailed: The audio file cannot be downloaded."
    )


def test_cloud_detail_redacts_signed_url_query() -> None:
    detail = "download https://bucket.example/audio.mp3?OSSAccessKeyId=id&Signature=secret failed"
    assert _safe_cloud_detail(detail) == "download https://bucket.example/audio.mp3?*** failed"

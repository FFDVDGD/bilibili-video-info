import json

import httpx

from cloud import AsrSettings, FunAsrClient, _extract_transcript, _safe_cloud_detail


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
    assert requests[1].headers["authorization"] == "Bearer sk-test"
    assert "authorization" not in requests[2].headers


def test_cloud_detail_redacts_signed_url_query() -> None:
    detail = "download https://bucket.example/audio.mp3?OSSAccessKeyId=id&Signature=secret failed"
    assert _safe_cloud_detail(detail) == "download https://bucket.example/audio.mp3?*** failed"

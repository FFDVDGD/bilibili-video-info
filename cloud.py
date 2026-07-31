"""OSS 临时对象与阿里云 Fun-ASR 异步任务。"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import oss2

_WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
_REGION_SUFFIXES = {
    "cn-beijing": "cn-beijing.maas.aliyuncs.com",
    "ap-southeast-1": "ap-southeast-1.maas.aliyuncs.com",
}


class CloudProcessingError(RuntimeError):
    """可安全展示的 OSS 或 ASR 错误。"""


@dataclass(frozen=True)
class OssSettings:
    endpoint: str
    bucket: str
    access_key_id: str
    access_key_secret: str
    object_prefix: str
    signed_url_ttl_seconds: int


@dataclass(frozen=True)
class AsrSettings:
    region: str
    workspace_id: str
    api_key: str
    timeout_seconds: int
    poll_interval_seconds: int


@dataclass(frozen=True)
class UploadedAudio:
    object_key: str
    signed_url: str


class OssAudioStore:
    """使用 OSS Python SDK 管理单个转写任务的临时音频。"""

    def __init__(self, settings: OssSettings) -> None:
        _require_value(settings.endpoint, "OSS endpoint")
        _require_value(settings.bucket, "OSS bucket")
        _require_value(settings.access_key_id, "OSS AccessKey ID")
        _require_value(settings.access_key_secret, "OSS AccessKey Secret")
        self.settings = settings
        auth = oss2.Auth(settings.access_key_id, settings.access_key_secret)
        self._bucket = oss2.Bucket(auth, settings.endpoint, settings.bucket)

    async def upload(self, audio_path: Path) -> UploadedAudio:
        prefix = self.settings.object_prefix.strip("/ ")
        name = f"{uuid.uuid4().hex}{audio_path.suffix.lower() or '.mp3'}"
        object_key = f"{prefix}/{name}" if prefix else name
        uploaded = False
        try:
            upload_task = asyncio.create_task(
                asyncio.to_thread(self._bucket.put_object_from_file, object_key, str(audio_path))
            )
            try:
                await asyncio.shield(upload_task)
            except asyncio.CancelledError:
                await upload_task
                uploaded = True
                raise
            uploaded = True
            signed_url = await asyncio.to_thread(
                self._bucket.sign_url,
                "GET",
                object_key,
                self.settings.signed_url_ttl_seconds,
            )
        except asyncio.CancelledError:
            if uploaded:
                with suppress(CloudProcessingError):
                    await asyncio.shield(self.delete(object_key))
            raise
        except Exception as exc:
            if uploaded:
                with suppress(CloudProcessingError):
                    await self.delete(object_key)
            raise CloudProcessingError(f"音频上传 OSS 失败：{_safe_cloud_error(exc)}") from exc
        return UploadedAudio(object_key=object_key, signed_url=str(signed_url))

    async def delete(self, object_key: str) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await asyncio.to_thread(self._bucket.delete_object, object_key)
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise CloudProcessingError(f"OSS 临时音频删除失败：{_safe_cloud_error(last_error)}") from last_error


class FunAsrClient:
    """通过 DashScope HTTP API 调用异步 ``fun-asr``。"""

    def __init__(self, client: httpx.AsyncClient, settings: AsrSettings) -> None:
        if settings.region not in _REGION_SUFFIXES:
            raise CloudProcessingError(f"不支持的百炼地域：{settings.region}")
        if not _WORKSPACE_ID_PATTERN.fullmatch(settings.workspace_id):
            raise CloudProcessingError("百炼 Workspace ID 为空或格式不正确")
        _require_value(settings.api_key, "百炼 API Key")
        self.client = client
        self.settings = settings
        suffix = _REGION_SUFFIXES[settings.region]
        self.base_url = f"https://{settings.workspace_id}.{suffix}/api/v1"

    async def transcribe(self, audio_url: str) -> str:
        task_id = await self._submit(audio_url)
        result_url = await self._wait_for_result(task_id)
        try:
            response = await self.client.get(result_url, timeout=30, follow_redirects=True)
        except httpx.RequestError as exc:
            raise CloudProcessingError("下载 Fun-ASR 转写结果失败：网络请求异常") from exc
        _raise_for_status(response, "下载 Fun-ASR 转写结果失败")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudProcessingError("Fun-ASR 转写结果不是有效 JSON") from exc
        transcript = _extract_transcript(payload)
        if not transcript:
            raise CloudProcessingError("Fun-ASR 返回了空转写结果")
        return transcript

    async def _submit(self, audio_url: str) -> str:
        try:
            response = await self.client.post(
                f"{self.base_url}/services/audio/asr/transcription",
                headers=self._headers(),
                json={
                    "model": "fun-asr",
                    "input": {"file_urls": [audio_url]},
                    "parameters": {"channel_id": [0]},
                },
                timeout=30,
            )
        except httpx.RequestError as exc:
            raise CloudProcessingError("提交 Fun-ASR 任务失败：网络请求异常") from exc
        _raise_for_status(response, "提交 Fun-ASR 任务失败")
        payload = _response_json(response, "Fun-ASR 提交响应")
        output = payload.get("output") if isinstance(payload, dict) else None
        task_id = str(output.get("task_id") or "") if isinstance(output, dict) else ""
        if not task_id:
            raise CloudProcessingError("Fun-ASR 提交响应中缺少 task_id")
        return task_id

    async def _wait_for_result(self, task_id: str) -> str:
        deadline = time.monotonic() + self.settings.timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(self.settings.poll_interval_seconds)
            try:
                response = await self.client.get(
                    f"{self.base_url}/tasks/{task_id}",
                    headers=self._headers(),
                    timeout=30,
                )
            except httpx.RequestError:
                continue
            if response.status_code >= 500 or response.status_code == 429:
                continue
            _raise_for_status(response, "查询 Fun-ASR 任务失败")
            payload = _response_json(response, "Fun-ASR 查询响应")
            output = payload.get("output") if isinstance(payload, dict) else None
            if not isinstance(output, dict):
                raise CloudProcessingError("Fun-ASR 查询响应中缺少 output")
            status = str(output.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                return _extract_result_url(output)
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                detail = _safe_cloud_detail(str(output.get("message") or output.get("code") or status))
                raise CloudProcessingError(f"Fun-ASR 任务失败：{detail}")
        raise CloudProcessingError(f"Fun-ASR 转写超过 {self.settings.timeout_seconds} 秒")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }


def _extract_result_url(output: dict[str, Any]) -> str:
    results = output.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise CloudProcessingError("Fun-ASR 成功响应中缺少 results")
    result = results[0]
    if str(result.get("subtask_status") or "").upper() not in {"", "SUCCEEDED"}:
        detail = _safe_cloud_detail(str(result.get("message") or result.get("code") or result.get("subtask_status")))
        raise CloudProcessingError(f"Fun-ASR 文件转写失败：{detail}")
    result_url = str(result.get("transcription_url") or "")
    if not result_url:
        raise CloudProcessingError("Fun-ASR 成功响应中缺少 transcription_url")
    return result_url


def _extract_transcript(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("transcripts"), list):
        return ""
    texts: list[str] = []
    for transcript in payload["transcripts"]:
        if not isinstance(transcript, dict):
            continue
        text = str(transcript.get("text") or "").strip()
        if text:
            texts.append(text)
            continue
        sentences = transcript.get("sentences")
        if isinstance(sentences, list):
            texts.extend(
                str(sentence.get("text") or "").strip()
                for sentence in sentences
                if isinstance(sentence, dict) and str(sentence.get("text") or "").strip()
            )
    return "\n".join(texts)


def _response_json(response: httpx.Response, label: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise CloudProcessingError(f"{label}不是有效 JSON") from exc


def _raise_for_status(response: httpx.Response, label: str) -> None:
    if response.is_success:
        return
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("code") or "")
    except ValueError:
        detail = response.text.strip()
    detail = _safe_cloud_detail(detail) or f"HTTP {response.status_code}"
    raise CloudProcessingError(f"{label}：{detail}")


def _require_value(value: str, label: str) -> None:
    if not value.strip():
        raise CloudProcessingError(f"未配置{label}")


def _safe_cloud_error(exc: Exception) -> str:
    return _safe_cloud_detail(str(exc)) or exc.__class__.__name__


def _safe_cloud_detail(detail: str) -> str:
    normalized = detail.replace("\n", " ").strip()
    normalized = re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]+", r"\1?***", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"(?i)(?:accesskeyid|ossaccesskeyid|signature|security-token|x-oss-signature)=([^&\s]+)",
        lambda match: match.group(0).split("=", 1)[0] + "=***",
        normalized,
    )
    return normalized[:200]

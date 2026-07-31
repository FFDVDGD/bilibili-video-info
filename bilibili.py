"""Bilibili 链接、yt-dlp 元数据和字幕处理。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx

_URL_PATTERN = re.compile(
    r"https?://(?:[A-Za-z0-9-]+\.)*(?:bilibili\.com|b23\.tv|bili2233\.cn)(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = "。！？；：，、,.!?;:)]}）】》〉」』"
_COOKIE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_ASS_TAG_PATTERN = re.compile(r"\{[^}]*}")
_TIMESTAMP_PATTERN = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{3}\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{3}"
)
_CHINESE_LANGUAGE_MARKERS = ("zh", "chi", "chs", "cht", "中文", "汉语", "國語", "国语")
_BILIBILI_RESOURCE_DOMAINS = ("bilibili.com", "hdslb.com", "bilivideo.com", "biliapi.net")


class BilibiliProcessingError(RuntimeError):
    """可安全展示给群聊的 Bilibili 处理错误。"""


class PlaylistNotSupportedError(BilibiliProcessingError):
    """链接指向批量列表而不是具体视频。"""


@dataclass(frozen=True)
class SubtitleTrack:
    """一个可下载的字幕轨道。"""

    language: str
    url: str
    extension: str
    automatic: bool


@dataclass(frozen=True)
class VideoMetadata:
    """总结工作流需要的视频元数据。"""

    source_url: str
    webpage_url: str
    video_key: str
    title: str
    description: str
    uploader: str
    published_at: str
    duration_seconds: int
    thumbnail_url: str
    subtitle_track: SubtitleTrack | None


@dataclass(frozen=True)
class ToolPaths:
    """插件随附二进制路径。"""

    yt_dlp: Path
    ffmpeg: Path

    @classmethod
    def discover(cls, plugin_dir: Path) -> ToolPaths:
        """按当前平台查找 ``bin`` 目录中的 yt-dlp 和 FFmpeg。"""

        binary_dir = plugin_dir / "bin"
        yt_dlp_names = ("yt-dlp.exe", "yt-dlp") if os.name == "nt" else ("yt-dlp",)
        ffmpeg_names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
        yt_dlp = _find_binary(binary_dir, yt_dlp_names)
        ffmpeg = _find_binary(binary_dir, ffmpeg_names)
        return cls(yt_dlp=yt_dlp, ffmpeg=ffmpeg)


def _find_binary(binary_dir: Path, names: tuple[str, ...]) -> Path:
    for name in names:
        candidate = binary_dir / name
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"未在 {binary_dir} 中找到 {' 或 '.join(names)}")


def extract_bilibili_url(text: str) -> str | None:
    """返回文本中的第一个受支持 Bilibili 视频链接。"""

    decoded_text = unescape(text)
    for match in _URL_PATTERN.finditer(decoded_text):
        candidate = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if _is_video_candidate(candidate):
            return candidate
    return None


def _is_video_candidate(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"}:
        return bool(parsed.path.strip("/"))
    if not (host == "bilibili.com" or host.endswith(".bilibili.com")):
        return False

    path = parsed.path.lower()
    if path.startswith("/video/"):
        return True
    if re.match(r"^/bangumi/play/(?:ep|ss)\d+", path):
        return True
    if path.startswith("/festival/"):
        query = parse_qs(parsed.query)
        return bool(query.get("bvid") or query.get("aid"))
    return False


def normalize_url_for_dedupe(url: str) -> str:
    """移除片段并统一主机大小写，生成初步去重键。"""

    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def write_netscape_cookie_file(cookie_header: str, destination: Path) -> None:
    """将浏览器 Cookie 请求头转换为 yt-dlp 可读取的 Netscape 文件。"""

    if "\n" in cookie_header or "\r" in cookie_header or "\t" in cookie_header:
        raise BilibiliProcessingError("Bilibili Cookie 必须是单行请求头字符串")

    cookie_pairs: list[tuple[str, str]] = []
    for item in cookie_header.split(";"):
        name, separator, value = item.strip().partition("=")
        if not separator or not name:
            continue
        if not _COOKIE_NAME_PATTERN.fullmatch(name):
            raise BilibiliProcessingError(f"Bilibili Cookie 中包含非法字段名：{name}")
        cookie_pairs.append((name, value))

    if not cookie_pairs:
        raise BilibiliProcessingError("Bilibili Cookie 中没有可用字段")

    lines = ["# Netscape HTTP Cookie File", "# Generated temporarily by bilibili-video-info", ""]
    lines.extend(f".bilibili.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}" for name, value in cookie_pairs)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    with suppress(OSError):
        destination.chmod(0o600)


class YtDlpClient:
    """通过用户放置的 yt-dlp 可执行文件提取 Bilibili 内容。"""

    def __init__(self, paths: ToolPaths, *, probe_timeout: int = 120, download_timeout: int = 900) -> None:
        self.paths = paths
        self.probe_timeout = probe_timeout
        self.download_timeout = download_timeout

    async def probe(self, url: str, work_dir: Path, cookie_file: Path | None) -> VideoMetadata:
        """读取单视频元数据，不下载媒体。"""

        if re.search(r"/bangumi/play/ss\d+", url, re.IGNORECASE):
            raise PlaylistNotSupportedError("该链接是番剧季度页，请发送具体的 ep 单集链接")

        arguments = [
            "--dump-single-json",
            "--skip-download",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--ffmpeg-location",
            str(self.paths.ffmpeg.parent),
        ]
        if cookie_file is not None:
            arguments.extend(("--cookies", str(cookie_file)))
        arguments.append(url)

        stdout = await self._run(arguments, work_dir=work_dir, timeout_seconds=self.probe_timeout)
        try:
            info = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BilibiliProcessingError("yt-dlp 返回了无法解析的元数据") from exc
        if not isinstance(info, dict):
            raise BilibiliProcessingError("yt-dlp 未返回有效的视频元数据")
        if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
            raise PlaylistNotSupportedError("该链接包含多个视频，请发送具体视频或具体分 P 链接")

        title = _clean_text(info.get("title")) or "未知标题"
        description = _clean_text(info.get("description"))
        uploader = _clean_text(info.get("uploader") or info.get("channel") or info.get("creator")) or "未知作者"
        webpage_url = str(info.get("webpage_url") or info.get("original_url") or url)
        duration_seconds = max(0, _to_int(info.get("duration")))
        video_key = _build_video_key(info, webpage_url)
        published_at = _format_published_at(info)
        thumbnail_url = str(info.get("thumbnail") or "").strip()
        subtitle_track = select_subtitle_track(info)
        return VideoMetadata(
            source_url=url,
            webpage_url=webpage_url,
            video_key=video_key,
            title=title,
            description=description,
            uploader=uploader,
            published_at=published_at,
            duration_seconds=duration_seconds,
            thumbnail_url=thumbnail_url,
            subtitle_track=subtitle_track,
        )

    async def download_audio(
        self,
        metadata: VideoMetadata,
        work_dir: Path,
        cookie_file: Path | None,
    ) -> Path:
        """下载最佳音频并转码为 MP3。"""

        output_template = work_dir / "audio.%(ext)s"
        arguments = [
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--ffmpeg-location",
            str(self.paths.ffmpeg.parent),
            "--format",
            "bestaudio/best",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "5",
            "--output",
            str(output_template),
        ]
        if cookie_file is not None:
            arguments.extend(("--cookies", str(cookie_file)))
        arguments.append(metadata.webpage_url)
        await self._run(arguments, work_dir=work_dir, timeout_seconds=self.download_timeout)

        audio_path = work_dir / "audio.mp3"
        if not audio_path.is_file():
            candidates = sorted(work_dir.glob("audio.*"))
            if not candidates:
                raise BilibiliProcessingError("yt-dlp 未生成音频文件")
            audio_path = candidates[0]
        return audio_path

    async def _run(self, arguments: list[str], *, work_dir: Path, timeout_seconds: int) -> str:
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            str(self.paths.yt_dlp),
            *arguments,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creation_flags,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        except TimeoutError as exc:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise BilibiliProcessingError(f"yt-dlp 处理超过 {timeout_seconds} 秒，已终止") from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise BilibiliProcessingError(_safe_yt_dlp_error(stderr))
        return stdout


def _safe_yt_dlp_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    detail = lines[-1] if lines else "未知错误"
    detail = re.sub(r"(?i)(SESSDATA|bili_jct|DedeUserID)=([^;\s]+)", r"\1=***", detail)
    if len(detail) > 240:
        detail = detail[:239] + "…"
    return f"B站视频解析失败：{detail}"


def _build_video_key(info: dict[str, Any], webpage_url: str) -> str:
    video_id = _clean_text(info.get("bvid") or info.get("id") or info.get("display_id")) or webpage_url
    part = _to_int(info.get("page") or info.get("playlist_index"))
    if part <= 0:
        query_part = parse_qs(urlsplit(webpage_url).query).get("p", ["1"])[0]
        part = max(1, _to_int(query_part))
    return f"{video_id}:p{part}"


def _format_published_at(info: dict[str, Any]) -> str:
    upload_date = str(info.get("upload_date") or "")
    if len(upload_date) == 8 and upload_date.isdigit():
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    timestamp = _to_int(info.get("timestamp") or info.get("release_timestamp"))
    if timestamp > 0:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    return "未知"


def select_subtitle_track(info: dict[str, Any]) -> SubtitleTrack | None:
    """按中文人工、中文自动、其他人工、其他自动顺序选择字幕。"""

    manual = _collect_tracks(info.get("subtitles"), automatic=False)
    automatic = _collect_tracks(info.get("automatic_captions"), automatic=True)
    groups = (
        [track for track in manual if _is_chinese_language(track.language)],
        [track for track in automatic if _is_chinese_language(track.language)],
        [track for track in manual if not _is_chinese_language(track.language)],
        [track for track in automatic if not _is_chinese_language(track.language)],
    )
    for group in groups:
        if group:
            return group[0]
    return None


def _collect_tracks(raw_tracks: Any, *, automatic: bool) -> list[SubtitleTrack]:
    if not isinstance(raw_tracks, dict):
        return []

    tracks: list[SubtitleTrack] = []
    for language, formats in raw_tracks.items():
        language_name = str(language)
        if "danmaku" in language_name.lower() or not isinstance(formats, list):
            continue
        candidates: list[SubtitleTrack] = []
        for item in formats:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            extension = str(item.get("ext") or "").lower()
            candidates.append(
                SubtitleTrack(language=language_name, url=url, extension=extension, automatic=automatic)
            )
        candidates.sort(key=lambda track: _subtitle_format_priority(track.extension))
        tracks.extend(candidates[:1])
    return tracks


def _subtitle_format_priority(extension: str) -> int:
    priorities = {"json": 0, "srt": 1, "vtt": 2, "ass": 3, "ssa": 3, "ttml": 4, "srv3": 5}
    return priorities.get(extension, 20)


def _is_chinese_language(language: str) -> bool:
    lowered = language.lower()
    return any(marker in lowered for marker in _CHINESE_LANGUAGE_MARKERS)


async def download_subtitle(
    client: httpx.AsyncClient,
    track: SubtitleTrack,
    metadata: VideoMetadata,
    cookie_header: str,
) -> str:
    """下载并转换字幕为纯文本。"""

    headers = {
        "Referer": metadata.webpage_url,
        "User-Agent": "Mozilla/5.0 (MaiBot bilibili-video-info)",
    }
    response = await _download_bilibili_resource(
        client,
        track.url,
        headers=headers,
        cookie_header=cookie_header,
        timeout_seconds=20,
    )
    response.raise_for_status()
    if len(response.content) > 20 * 1024 * 1024:
        raise BilibiliProcessingError("字幕文件超过 20MB")
    return parse_subtitle(response.content, track.extension)


def parse_subtitle(content: bytes, extension: str) -> str:
    """解析 Bilibili JSON、SRT、VTT 或 ASS 字幕。"""

    text = content.decode("utf-8-sig", errors="replace")
    if extension == "json" or text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            parsed = _parse_json_subtitle(data)
            if parsed:
                return parsed
    return _parse_text_subtitle(text, extension)


def _parse_json_subtitle(data: Any) -> str:
    if isinstance(data, dict):
        body = data.get("body")
        if body is None and isinstance(data.get("data"), dict):
            body = data["data"].get("body")
        if isinstance(body, list):
            return _join_subtitle_lines(item.get("content", "") for item in body if isinstance(item, dict))

        events = data.get("events")
        if isinstance(events, list):
            lines: list[str] = []
            for event in events:
                if not isinstance(event, dict) or not isinstance(event.get("segs"), list):
                    continue
                lines.append(
                    "".join(
                        str(segment.get("utf8") or "")
                        for segment in event["segs"]
                        if isinstance(segment, dict)
                    )
                )
            return _join_subtitle_lines(lines)
    if isinstance(data, list):
        return _join_subtitle_lines(
            item.get("content") or item.get("text") or "" for item in data if isinstance(item, dict)
        )
    return ""


def _parse_text_subtitle(text: str, extension: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or line.isdigit() or _TIMESTAMP_PATTERN.match(line):
            continue
        if line.startswith(("NOTE", "STYLE", "REGION", "Kind:", "Language:")):
            continue
        if extension in {"ass", "ssa"}:
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            line = parts[9]
        line = line.replace("\\N", " ").replace("\\n", " ")
        line = _ASS_TAG_PATTERN.sub("", line)
        line = _HTML_TAG_PATTERN.sub("", line)
        lines.append(unescape(line))
    return _join_subtitle_lines(lines)


def _join_subtitle_lines(lines: Any) -> str:
    normalized: list[str] = []
    for line in lines:
        cleaned = _clean_text(line)
        if cleaned and (not normalized or cleaned != normalized[-1]):
            normalized.append(cleaned)
    return "\n".join(normalized)


async def download_cover(client: httpx.AsyncClient, metadata: VideoMetadata) -> bytes | None:
    """下载封面；没有封面时返回 ``None``。"""

    if not metadata.thumbnail_url:
        return None
    response = await _download_bilibili_resource(
        client,
        metadata.thumbnail_url,
        headers={"Referer": metadata.webpage_url, "User-Agent": "Mozilla/5.0 (MaiBot bilibili-video-info)"},
        cookie_header="",
        timeout_seconds=10,
    )
    response.raise_for_status()
    if len(response.content) > 10 * 1024 * 1024:
        raise BilibiliProcessingError("视频封面超过 10MB")
    return response.content


async def _download_bilibili_resource(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    cookie_header: str,
    timeout_seconds: int,
) -> httpx.Response:
    """只从 Bilibili 官方域名下载资源，并避免跨域携带登录 Cookie。"""

    current_url = _normalize_bilibili_resource_url(url)
    for _redirect_count in range(4):
        host = (urlsplit(current_url).hostname or "").lower()
        request_headers = dict(headers)
        if cookie_header.strip() and _host_matches_domain(host, "bilibili.com"):
            request_headers["Cookie"] = cookie_header.strip()
        response = await client.get(
            current_url,
            headers=request_headers,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        if not location:
            raise BilibiliProcessingError("Bilibili 资源重定向缺少目标地址")
        current_url = _normalize_bilibili_resource_url(urljoin(current_url, location))
    raise BilibiliProcessingError("Bilibili 资源重定向次数过多")


def _normalize_bilibili_resource_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise BilibiliProcessingError("Bilibili 资源 URL 格式不正确")
    if parsed.username or parsed.password:
        raise BilibiliProcessingError("Bilibili 资源 URL 不允许包含用户信息")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BilibiliProcessingError("Bilibili 资源 URL 端口格式不正确") from exc
    if port not in {None, 80, 443}:
        raise BilibiliProcessingError("Bilibili 资源 URL 使用了非标准端口")
    if not any(_host_matches_domain(host, domain) for domain in _BILIBILI_RESOURCE_DOMAINS):
        raise BilibiliProcessingError("Bilibili 资源 URL 指向了非官方域名")
    scheme = "https"
    netloc = host if port in {None, 80, 443} else parsed.netloc
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))


def _host_matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def format_metadata_text(metadata: VideoMetadata, description_limit: int) -> str:
    """生成适合聊天软件展示的纯文本元数据。"""

    description = _truncate(metadata.description or "无", description_limit)
    duration = format_duration(metadata.duration_seconds)
    return (
        f"B站视频\n"
        f"标题：{metadata.title}\n"
        f"作者：{metadata.uploader}\n"
        f"发布时间：{metadata.published_at}\n"
        f"时长：{duration}\n"
        f"简介：{description}"
    )


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "未知"
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes}:{remaining_seconds:02d}"


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u200b", "").split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0

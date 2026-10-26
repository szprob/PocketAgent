"""用户上传的附件（图片、文件）。

文件存在 ~/.pocketagent/attachments/<会话 id>/，消息里只放引用（pocketagent-attachment:<会话>/<文件>），
发给模型前由 materialize_messages 展开：能看图的模型换成 base64 的 data URL，不能看图的换成一句说明。
文本附件直接把内容拼进消息；其他文件只告诉模型保存的路径，需要时它用工具去看。

user 消息里另外带一个 _display 字段（原话 + 附件清单），给界面重放历史用；发给模型前会去掉。
"""

import base64
import copy
import mimetypes
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ATTACHMENT_URL_PREFIX = "pocketagent-attachment:"
TEXT_INLINE_LIMIT_CHARS = 50_000
TEXT_DETECTION_MAX_BYTES = 2_000_000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
DISPLAY_KEY = "_display"


@dataclass
class Attachment:
    # 磁盘上的文件名（随机前缀 + 原名），会话内唯一，也是引用里用的 id
    id: str
    name: str
    # image / text / binary
    kind: str
    mime: str
    size: int
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_file_name(original_name: str) -> str:
    base_name = Path(original_name.replace("\\", "/")).name or "file"
    cleaned = re.sub(r"[\x00-\x1f/:]", "_", base_name).strip(". ") or "file"
    return cleaned[-100:]


def _detect_kind(data: bytes, mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if len(data) <= TEXT_DETECTION_MAX_BYTES:
        try:
            data.decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            pass
    return "binary"


class AttachmentStore:
    def __init__(self, root: Path, session_id: str) -> None:
        self.session_id = session_id
        self.directory = root / session_id

    def save(self, original_name: str, data: bytes) -> Attachment:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"附件太大（{len(data)} 字节），上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
        name = _safe_file_name(original_name)
        attachment_id = f"{uuid.uuid4().hex[:8]}-{name}"
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / attachment_id
        path.write_bytes(data)
        return self.get(attachment_id)

    def get(self, attachment_id: str) -> Attachment:
        """找不到抛 KeyError。"""
        if "/" in attachment_id or attachment_id.startswith("."):
            raise KeyError(attachment_id)
        path = self.directory / attachment_id
        if not path.is_file():
            raise KeyError(attachment_id)
        data = path.read_bytes()
        name = attachment_id.split("-", 1)[1] if "-" in attachment_id else attachment_id
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Attachment(
            id=attachment_id,
            name=name,
            kind=_detect_kind(data, mime),
            mime=mime,
            size=len(data),
            path=str(path),
        )

    def url(self, attachment: Attachment) -> str:
        return f"{ATTACHMENT_URL_PREFIX}{self.session_id}/{attachment.id}"


def _text_section(attachment: Attachment) -> str:
    content = Path(attachment.path).read_text(encoding="utf-8")
    truncated_note = ""
    if len(content) > TEXT_INLINE_LIMIT_CHARS:
        content = content[:TEXT_INLINE_LIMIT_CHARS]
        truncated_note = f"\n（只附了前 {TEXT_INLINE_LIMIT_CHARS} 个字符，完整内容用 read_file 读上面的路径）"
    return f"附件 {attachment.name}（本机路径 {attachment.path}）：\n```\n{content}\n```{truncated_note}"


def build_user_message(text: str, attachments: list[Attachment], store: AttachmentStore) -> dict[str, Any]:
    """把用户的话和附件拼成一条 user 消息。

    有图片时 content 是分段列表：用户的话 → 图片 → 其他附件说明；没有图片就是一个字符串。
    图片段里带着内部字段 _name / _path，展开时用来写「你能直接看到这张图」或「当前模型不能看图」。
    （实测把「本机路径」写在图片前面，模型会以为自己看不到图、跑去用工具读文件。）
    """
    image_parts = []
    note_sections = []
    for attachment in attachments:
        if attachment.kind == "image":
            image_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": store.url(attachment)},
                    "_name": attachment.name,
                    "_path": attachment.path,
                }
            )
        elif attachment.kind == "text":
            note_sections.append(_text_section(attachment))
        else:
            note_sections.append(
                f"[附件 {attachment.name}（{attachment.mime}，{attachment.size} 字节）保存在 {attachment.path}，需要时用工具查看]"
            )
    content: str | list[dict[str, Any]] = "\n\n".join(([text] if text else []) + note_sections)
    if image_parts:
        content = [{"type": "text", "text": text or "（见附件）"}] + image_parts
        if note_sections:
            content.append({"type": "text", "text": "\n\n".join(note_sections)})
    display = {
        "text": text,
        "attachments": [
            {"id": item.id, "name": item.name, "kind": item.kind, "size": item.size} for item in attachments
        ],
    }
    return {"role": "user", "content": content, DISPLAY_KEY: display}


def message_display(message: dict[str, Any]) -> dict[str, Any]:
    """界面上怎么显示这条 user 消息：原话 + 附件清单。老消息没有 _display 就从 content 里取文字。"""
    if DISPLAY_KEY in message:
        return message[DISPLAY_KEY]
    return {"text": message_text(message), "attachments": []}


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [part.get("text", "") for part in content if part.get("type") == "text"]
        return "\n".join(texts)
    return ""


def _image_data_url(attachments_root: Path | None, reference: str) -> str | None:
    if attachments_root is None:
        return None
    relative = reference[len(ATTACHMENT_URL_PREFIX) :]
    path = (attachments_root / relative).resolve()
    if attachments_root.resolve() not in path.parents or not path.is_file():
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _materialize_part(
    part: dict[str, Any], attachments_root: Path | None, supports_images: bool
) -> list[dict[str, Any]]:
    """一段展开成一段或几段：图片引用换成 data URL 并紧跟一句「你能直接看到」，其他段原样（去掉内部字段）。"""
    cleaned = {key: value for key, value in part.items() if not key.startswith("_")}
    if part.get("type") != "image_url":
        return [cleaned]
    url = part.get("image_url", {}).get("url", "")
    if not url.startswith(ATTACHMENT_URL_PREFIX):
        return [cleaned]
    name = part.get("_name", "图片")
    path = part.get("_path", "")
    if not supports_images:
        return [{"type": "text", "text": f"[用户附带了图片 {name}（本机 {path}），当前模型不能看图，没有发送图片内容]"}]
    data_url = _image_data_url(attachments_root, url)
    if data_url is None:
        return [{"type": "text", "text": f"[用户附带的图片 {name} 已经不在了]"}]
    note = f"（上面是用户附带的图片 {name}，你能直接看到，不需要再用工具读这个文件；本机路径 {path}）"
    return [{"type": "image_url", "image_url": {"url": data_url}}, {"type": "text", "text": note}]


def materialize_messages(
    messages: list[dict[str, Any]], attachments_root: Path | None, supports_images: bool
) -> list[dict[str, Any]]:
    """发给模型前的最后一步：去掉内部字段，把附件引用展开。不改原消息。"""
    materialized = []
    for message in messages:
        cleaned = {key: value for key, value in message.items() if not key.startswith("_")}
        content = cleaned.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                parts.extend(_materialize_part(copy.deepcopy(part), attachments_root, supports_images))
            cleaned["content"] = parts
        materialized.append(cleaned)
    return materialized

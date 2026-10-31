# -*- coding: utf-8 -*-
"""图片懒加载：图片落盘编号，上下文只留占位，LLM 需要时用 view_image 取一张。

解决的问题：
  聊天里图片一旦进了上下文，就会以 base64 每轮重发一遍（几 MB / 上千 token），
  带宽低的服务器上光传输就要几十秒。这里把图挪到磁盘，上下文里只留 [图#12]。

用法：
  用户发图 → 落盘编号 → 上下文只留 [图#12]
  模型需要看内容 → 调 view_image(12) → 下一轮请求才把这一张（缩到 512）发过去
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:  # 缩图用，没有就退回原图
    from PIL import Image as PILImage
except Exception:  # pragma: no cover
    PILImage = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_lazy_image"

INJECT_HINT = "【会话图片】最近可用编号：{ids}。只有需要辨认图里的细节/文字/颜色时，才调 view_image(image_id) 取原图；只提到「那张图」而不用细节时不要调。"


class ImageStore:
    """按会话存图 + 编号索引。"""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    def session_dir(self, umo: str) -> Path:
        key = hashlib.sha1((umo or "default").encode("utf-8")).hexdigest()[:16]
        p = self.base / key
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _index_path(self, sdir: Path) -> Path:
        return sdir / "index.json"

    def load(self, umo: str) -> dict:
        sdir = self.session_dir(umo)
        path = self._index_path(sdir)
        if not path.exists():
            return {"last_id": 0, "items": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("bad index")
            data.setdefault("last_id", 0)
            data.setdefault("items", [])
            return data
        except Exception:
            logger.warning("[lazyimg] 索引损坏，重建：%s", path)
            return {"last_id": 0, "items": []}

    def save(self, umo: str, index: dict) -> None:
        sdir = self.session_dir(umo)
        self._index_path(sdir).write_text(
            json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def find_by_sha(self, index: dict, sha: str) -> dict | None:
        for item in index.get("items", []):
            if item.get("sha") == sha:
                return item
        return None

    def add(self, umo: str, data: bytes, ext: str, sha: str, size: tuple[int, int]) -> dict:
        index = self.load(umo)
        new_id = int(index.get("last_id", 0)) + 1
        index["last_id"] = new_id
        sdir = self.session_dir(umo)
        name = f"{new_id}.{ext}"
        (sdir / name).write_bytes(data)
        item = {
            "id": new_id,
            "file": name,
            "sha": sha,
            "w": size[0],
            "h": size[1],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        index.setdefault("items", []).append(item)
        self.save(umo, index)
        return item

    def path_of(self, umo: str, item: dict) -> Path:
        return self.session_dir(umo) / str(item.get("file") or "")

    def llm_copy(self, umo: str, item: dict, max_side: int) -> Path:
        """给 LLM 看的缩略图（缓存）。没装 Pillow 就退回原图。"""
        src = self.path_of(umo, item)
        if PILImage is None:
            return src
        dst = self.session_dir(umo) / f"{item.get('id')}_llm.jpg"
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        try:
            img = PILImage.open(src)
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if max(img.size) > max_side:
                scale = max_side / float(max(img.size))
                img = img.resize(
                    (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))),
                    PILImage.LANCZOS,
                )
            img.save(dst, "JPEG", quality=85)
            return dst
        except Exception:  # pragma: no cover - 动图/损坏文件
            logger.debug("[lazyimg] 缩图失败，用原图", exc_info=True)
            return src

    def cleanup(self, days: int) -> int:
        """删掉超过 N 天的图（0 = 不删）。"""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        removed = 0
        for sdir in self.base.iterdir():
            if not sdir.is_dir():
                continue
            index_path = sdir / "index.json"
            if not index_path.exists():
                continue
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            keep = []
            for item in index.get("items", []):
                stale = False
                for name in (item.get("file"), f"{item.get('id')}_llm.jpg"):
                    p = sdir / str(name or "")
                    try:
                        if p.exists() and p.stat().st_mtime < cutoff:
                            p.unlink()
                            stale = True
                    except OSError:
                        pass
                if not stale:
                    keep.append(item)
            removed += len(index.get("items", [])) - len(keep)
            index["items"] = keep
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        return removed


@register(
    PLUGIN_NAME,
    "dahaoiua",
    "图片懒加载：图片只落盘编号，上下文里不放 base64，LLM 需要时调 view_image 取一张",
    "v1.0.0",
)
class LazyImagePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = config if isinstance(config, dict) else {}
        self.config = cfg
        self.store = ImageStore(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME)
        self._pending: dict[str, list[str]] = {}
        self._last_cleanup = 0.0
        # umo -> (最近落盘的 id, 时间戳)，用来在下一轮把 [图#N] 补进消息里
        self._last_capture: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------ 配置读取
    def _bool(self, key: str, default: bool) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _str(self, key: str, default: str = "") -> str:
        val = self.config.get(key, default)
        return default if val is None else str(val)

    def _session_allowed(self, event: AstrMessageEvent) -> bool:
        umo = getattr(event, "unified_msg_origin", "") or ""
        blocked = str(self.config.get("blocked_sessions", "") or "")
        allowed = str(self.config.get("allowed_sessions", "") or "")
        if blocked and any(part.strip() and part.strip() in umo for part in blocked.split(",")):
            return False
        if allowed:
            keys = [p.strip() for p in allowed.split(",") if p.strip()]
            return any(k in umo for k in keys)
        return True

    # ------------------------------------------------------------ 图片处理
    @staticmethod
    def _read_ref(ref: str) -> tuple[bytes, str]:
        ref = (ref or "").strip()
        if not ref:
            return b"", ""
        if ref.startswith("data:"):
            head, _, payload = ref.partition(",")
            ext = "png" if "png" in head else ("gif" if "gif" in head else "jpg")
            try:
                return base64.b64decode(payload), ext
            except Exception:
                return b"", ""
        if ref.startswith(("http://", "https://")):
            try:
                with urllib.request.urlopen(ref, timeout=30) as resp:
                    data = resp.read()
                ext = "png" if data[:8].startswith(b"\x89PNG") else "jpg"
                return data, ext
            except Exception:
                logger.debug("[lazyimg] 下载图片失败 %s", ref[:80], exc_info=True)
                return b"", ""
        path = Path(ref)
        if path.is_file():
            suffix = path.suffix.lower().lstrip(".") or "jpg"
            if suffix == "jpeg":
                suffix = "jpg"
            try:
                return path.read_bytes(), suffix
            except OSError:
                return b"", ""
        return b"", ""

    @staticmethod
    def _probe_size(data: bytes) -> tuple[int, int]:
        if PILImage is None:
            return (0, 0)
        try:
            from io import BytesIO

            with PILImage.open(BytesIO(data)) as img:
                return img.size
        except Exception:
            return (0, 0)

    def _register(self, umo: str, refs: list[str]) -> list[dict]:
        """把当前消息里的图落盘编号，返回已登记的条目。"""
        limit = self._int("max_images_per_message", 3)
        registered: list[dict] = []
        index = None
        for ref in refs[: max(1, limit)]:
            data, ext = self._read_ref(ref)
            if not data:
                continue
            sha = hashlib.sha256(data).hexdigest()
            index = index or self.store.load(umo)
            hit = self.store.find_by_sha(index, sha)
            if hit:
                registered.append(hit)
                continue
            item = self.store.add(umo, data, ext, sha, self._probe_size(data))
            index["items"].append(item) if index is not None else None
            registered.append(item)
            logger.info("[lazyimg] 图片落盘 %s #%s %sx%s", umo, item["id"], item["w"], item["h"])
        return registered

    @staticmethod
    def _strip_contexts(req) -> int:
        """把历史里的 image_url 块换成文字占位，返回替换数量。"""
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return 0
        replaced = 0
        for msg in contexts:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for idx, part in enumerate(content):
                    if isinstance(part, dict) and part.get("type") in ("image_url", "input_image"):
                        content[idx] = {"type": "text", "text": "[图片]"}
                        replaced += 1
            elif isinstance(content, str) and "data:image" in content:
                new = re.sub(
                    r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+",
                    "[图片]",
                    content,
                )
                if new != content:
                    msg["content"] = new
                    replaced += 1
        return replaced

    def _push_pending(self, umo: str, path: str) -> None:
        self._pending.setdefault(umo, []).append(path)

    def _pop_pending(self, umo: str) -> list[str]:
        return self._pending.pop(umo, [])

    def _recent_items(self, umo: str, limit: int = 5) -> list[dict]:
        items = self.store.load(umo).get("items", [])
        return items[-limit:] if limit > 0 else []

    # ------------------------------------------------------------ LLM 请求钩子
    @filter.on_llm_request()
    async def lazy_images(self, event: AstrMessageEvent, req) -> None:
        if not self._bool("enabled", True):
            return
        if not self._session_allowed(event):
            return
        umo = getattr(event, "unified_msg_origin", "") or ""

        # 1) 当前消息里的图：落盘编号，然后从请求里摘掉
        current_refs = list(getattr(req, "image_urls", None) or [])
        new_items = self._register(umo, current_refs) if current_refs else []
        prompt = getattr(req, "prompt", "") or ""
        if new_items:
            tags = " ".join(f"[图#{it['id']}]" for it in new_items)
            if tags not in prompt:
                prompt = (prompt + f"\n{tags}").strip()
        else:
            # 「先发图、再单独提问」：图已经在消息级落过盘，把编号补进这句话里
            cap = self._last_capture.get(umo)
            if cap:
                cap_id, cap_ts = cap
                fresh = time.time() - cap_ts <= self._int("capture_hint_seconds", 180)
                tag = f"[图#{cap_id}]"
                if fresh and tag not in prompt:
                    prompt = (prompt + f"\n{tag}").strip()
        req.prompt = prompt

        # 2) 历史里的图片块一律换成占位
        stripped = self._strip_contexts(req)
        if stripped:
            logger.debug("[lazyimg] 历史图片块替换 %s 处", stripped)

        # 3) 当前消息的图：只有开了 keep_current_image 才真的带给模型（需要模型自己支持视觉）
        keep: list[str] = []
        limit = max(1, self._int("max_images_per_request", 2))
        if new_items and self._bool("keep_current_image", False):
            for item in new_items:
                path = self.store.llm_copy(umo, item, self._int("llm_max_side", 512))
                keep.append(str(path))
        req.image_urls = keep[:limit]

        # 4) 告诉模型有哪些图可以申请
        if self._bool("inject_hint", True):
            recent = self._recent_items(umo, self._int("hint_items", 5))
            if recent:
                ids = "、".join(f"#{it['id']}({it['w']}x{it['h']})" for it in recent)
                hint = INJECT_HINT.format(ids=ids)
                req.system_prompt = ((getattr(req, "system_prompt", "") or "") + "\n\n" + hint).strip()

    # ------------------------------------------------------------ 消息级落盘
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def capture_message_images(self, event: AstrMessageEvent) -> None:
        """每条消息都过一遍：图先落盘编号，不管这条消息有没有唤醒机器人。

        否则「先发图、再单独提问」的场景里，图根本没机会被登记。
        """
        if not self._bool("enabled", True):
            return
        if not self._session_allowed(event):
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        message_obj = getattr(event, "message_obj", None)
        refs: list[str] = []
        for comp in getattr(message_obj, "message", []) or []:
            if isinstance(comp, Image):
                try:
                    refs.append(await comp.convert_to_file_path())
                except Exception:  # pragma: no cover - 图片过期/下载失败
                    logger.debug("[lazyimg] 消息图片取不到", exc_info=True)
        if not refs:
            return
        items = self._register(umo, refs)
        if items:
            self._last_capture[umo] = (int(items[-1]["id"]), time.time())

    # ------------------------------------------------------------ 工具：按需看图
    def _vision_provider(self, event: AstrMessageEvent):
        """看图用的视觉模型：插件配置 > provider_settings 的 caption provider > 当前对话模型。"""
        pid = self._str("vision_provider", "").strip()
        if pid:
            prov = self.context.get_provider_by_id(pid)
            if prov is not None:
                return prov
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin) or {}
            cid = (cfg.get("provider_settings") or {}).get("default_image_caption_provider_id") or ""
        except Exception:
            cid = ""
        if cid:
            prov = self.context.get_provider_by_id(cid)
            if prov is not None:
                return prov
        try:
            return self.context.get_using_provider(umo=event.unified_msg_origin)
        except Exception:
            return None

    @filter.llm_tool(name="view_image")
    async def view_image(
        self,
        event: AstrMessageEvent,
        image_id: int = 0,
        question: str | None = None,
    ) -> str:
        """看清某张图的内容（由视觉模型转述成文字返回）。

        需要知道图里画了什么、有什么文字、什么颜色、什么物体时调用；只提到「那张图」而不用细节时不要调。

        Args:
            image_id(int): 图片编号，即消息里 [图#12] 的 12；不传或传 0 表示最近一张
            question(string): 可选，具体想问什么，例如"画面里有几个人""招牌上写的什么字"
        """
        if not self._bool("enabled", True):
            return "图片懒加载已关闭。"
        umo = getattr(event, "unified_msg_origin", "") or ""
        index = self.store.load(umo)
        items = index.get("items", [])
        if not items:
            return "本会话还没有收到过图片。"
        if image_id:
            target = next((it for it in items if int(it.get("id", -1)) == int(image_id)), None)
            if target is None:
                return f"没有编号 {image_id} 的图片。可用：{', '.join('#'+str(it['id']) for it in items[-5:])}"
        else:
            target = items[-1]

        question = (question or "").strip()
        cached = (target.get("caption") or "").strip()
        if cached and not question:
            return f"图#{target.get('id')}（{target.get('w')}x{target.get('h')}）的内容：{cached}"

        prov = self._vision_provider(event)
        if prov is None:
            return "看图失败：没有可用的视觉模型（检查 provider_settings.default_image_caption_provider_id 或插件配置 vision_provider）。"

        path = self.store.llm_copy(umo, target, self._int("vision_max_side", 768))
        base_prompt = self._str(
            "vision_prompt",
            "仔细看这张图，用中文回答：画面主体是什么、场景在哪、有什么文字（原样写出）、主色调和构图。只描述看得到的内容，不要猜。",
        )
        prompt = f"{base_prompt}\n\n额外问题：{question}" if question else base_prompt
        text = ""
        try:
            resp = await prov.text_chat(prompt=prompt, image_urls=[str(path)])
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning("[lazyimg] 看图失败：%s", exc)
            return f"看图失败：{exc}"
        if not text:
            # 推理模型偶尔把 token 全用在思考上，压一问再试一次
            try:
                resp = await prov.text_chat(
                    prompt="用中文简短描述这张图：主体、场景、画面里的文字、主色调。",
                    image_urls=[str(path)],
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as exc:
                logger.warning("[lazyimg] 看图重试失败：%s", exc)
        if not text:
            return "看图失败：视觉模型没返回内容（可能是推理 token 用超了，稍后再试）。"

        if not question and self._bool("cache_caption", True):
            target["caption"] = text[:1000]
            self.store.save(umo, index)
        logger.info("[lazyimg] 看图 #%s：%s", target.get("id"), text[:60].replace("\n", " "))
        return f"图#{target.get('id')}（{target.get('w')}x{target.get('h')}）的内容：\n{text}"

    # ------------------------------------------------------------ 命令
    @filter.command("img_list")
    async def cmd_img_list(self, event: AstrMessageEvent):
        """看本会话保存了哪些图片。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        items = self.store.load(umo).get("items", [])
        if not items:
            yield event.plain_result("本会话还没有图片。")
            return
        lines = [f"共 {len(items)} 张："]
        for it in items[-10:]:
            lines.append(f"#{it['id']}  {it['w']}x{it['h']}  {it.get('at', '')}")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @filter.command("img_show")
    async def cmd_img_show(self, event: AstrMessageEvent, image_id: str = ""):
        """把某张图重新发出来：/img_show 12。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        items = self.store.load(umo).get("items", [])
        if not items:
            yield event.plain_result("本会话还没有图片。")
            event.stop_event()
            return
        try:
            target = next((it for it in items if int(it.get("id")) == int(image_id)), None) if image_id else items[-1]
        except ValueError:
            target = None
        if target is None:
            yield event.plain_result("没找到这张图。")
            event.stop_event()
            return
        path = self.store.path_of(umo, target)
        from astrbot.api.message_components import Image

        yield event.chain_result([Image.fromFileSystem(str(path))])
        event.stop_event()

    @filter.command("img_clean")
    async def cmd_img_clean(self, event: AstrMessageEvent):
        """按保留天数清理旧图。"""
        removed = self.store.cleanup(self._int("retention_days", 7))
        yield event.plain_result(f"已清理 {removed} 张过期的图。")
        event.stop_event()

    async def terminate(self) -> None:
        try:
            self.store.cleanup(self._int("retention_days", 7))
        except Exception:
            logger.debug("[lazyimg] 退出清理失败", exc_info=True)

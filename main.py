"""AstrBot Z-Library 找书插件

命令：
    找书 <书名/作者/关键词>   在 Z-Library 搜索书籍
    选书 <编号>               下载搜索结果中的某本书，并把文件发给找书人
    找书检测                  检查各站点的 DNS、连通性与登录状态
"""

import asyncio
import json
import re
import socket
import time
from pathlib import Path

import aiohttp

try:
    # Cloudflare / DiamWall 会按 TLS 指纹拦截普通 HTTP 客户端，
    # curl_cffi 能模拟浏览器指纹，优先使用它。
    from curl_cffi.requests import AsyncSession as _CffiSession

    _HAS_CFFI = True
except Exception:  # pragma: no cover - 没装 curl_cffi 时退回 aiohttp
    _CffiSession = None
    _HAS_CFFI = False

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:
    # GreedyStr 让指令参数吃掉剩余全部文本；老版本 AstrBot 里没有这个类，
    # 缺失时退回 str，此时用 _full_query() 从原始消息里补全多词查询。
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover
    GreedyStr = str

PLUGIN_NAME = "astrbot_plugin_zlibrary"
RESULT_TTL = 30 * 60  # 搜索结果缓存时长（秒）
DOWNLOAD_KEEP_HOURS = 24  # 已下载文件的本地保留时长（小时）

TROUBLESHOOT = (
    "\n\n排查建议：\n"
    "1) 发「找书检测」看每个域名的状态：显示「浏览器验证」的域名不能用\n"
    "2) 站点域名会变，把 base_url 换成能直接调接口的域名，可填多个（逗号分隔），"
    "例如 https://z-library.ec, https://z-library.sk\n"
    "3) 服务器直连不到 Z-Library（DNS 被污染或被拦截）时，在插件配置 proxy 里填代理，"
    "例如 http://127.0.0.1:7897\n"
    "4) 若提示缺少 curl-cffi，重载插件让 AstrBot 安装依赖，或在服务器上 pip install curl-cffi"
)

UNSUPPORTED = "__protocol_unsupported__"

CHALLENGE_HINT = (
    "被浏览器验证拦截（Cloudflare / DiamWall），普通 HTTP 请求拿不到数据，请换用其他域名"
)

DEFAULT_VISION_PROMPT = (
    "你是图书识别助手。请识别这张图片里书籍封面上的书名和作者（作者可能是中文或英文）。\n"
    "只输出一行 JSON，不要输出任何解释：\n"
    '{"title": "书名", "author": "作者"}\n'
    "如果看不清作者就留空；如果图片里不是书或完全无法辨认，输出 "
    '{"title": "", "author": ""}。'
)


def _explain_llm_error(exc: Exception) -> str:
    """把模型报错翻译成人能看懂的短句。"""
    text = str(exc)
    low = text.lower()
    if "429" in text or "1305" in text or "访问量" in text or "rate limit" in low:
        return "模型繁忙/被限流（429），稍后再试或换一个模型"
    if "401" in text or "403" in text or "auth" in low or "身份验证" in text:
        return "鉴权失败，检查该模型的 API Key 配置"
    if "does not support image" in low or "unsupported image" in low:
        return "该模型不支持图片输入"
    return text.strip()[:120] or type(exc).__name__


def _parse_book_guess(text: str) -> tuple[str, str]:
    """从模型的回答里抽出书名和作者，尽量容忍模型不按 JSON 输出。"""
    raw = (text or "").strip()
    if not raw:
        return "", ""
    match = re.search(r"\{[^{}]*\}", raw, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
        if isinstance(data, dict) and "title" in data:
            title = str(data.get("title") or "").strip()
            author = str(data.get("author") or "").strip()
            return title[:100], author[:60]
    quoted = re.search(r"《([^》]{1,100})》", raw)
    if quoted:
        author = ""
        am = re.search(r"作者[:：]\s*([^\n，,。]{1,60})", raw)
        if am:
            author = am.group(1).strip()
        return quoted.group(1).strip(), author
    # 推理模型常把结论写在 reasoning_content 里，形如「主标题：**xxx**」
    labelled = re.search(
        r"(?:主标题|书名|标题|title)\s*[:：]\s*\**\s*([^\n*，,。]{1,80})", raw, re.I
    )
    if labelled:
        title = labelled.group(1).strip(" 《》\"'*")
        author = ""
        am = re.search(r"作者\s*[:：]\s*\**\s*([^\n*，,。]{1,60})", raw)
        if am:
            author = am.group(1).strip(" *")
        if title:
            return title[:100], author[:60]
    first = raw.splitlines()[0].strip()
    first = re.sub(r"^(书名|标题|title)[:：]\s*", "", first, flags=re.I)
    first = first.strip(" 《》\"'")
    if not first or len(first) > 100:
        return "", ""
    author = ""
    am = re.search(r"作者[:：]\s*([^\n，,。]{1,60})", raw)
    if am:
        author = am.group(1).strip()
    return first, author


def _host(url: str) -> str:
    return re.sub(r"^https?://([^/]+).*$", r"\1", url)


def _parse_domains(raw) -> list[str]:
    """支持用逗号/空格/换行分隔多个候选域名，按顺序尝试。"""
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[,，\s]+", str(raw or ""))
    out: list[str] = []
    for part in parts:
        part = part.strip().rstrip("/")
        if not part:
            continue
        if not part.startswith(("http://", "https://")):
            part = "https://" + part
        if part not in out:
            out.append(part)
    return out


def _looks_like_challenge(text: str) -> bool:
    """识别 DiamWall / Cloudflare 之类的反爬验证页。"""
    low = (text or "").lower()
    return any(
        marker in low
        for marker in (
            "diamwall",
            "verifying your browser",
            "just a moment",
            "cf-mitigated",
            "attention required! | cloudflare",
        )
    )


def _human_size(num) -> str:
    try:
        num = float(num)
    except (TypeError, ValueError):
        return ""
    if num <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{int(num)} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return ""


def _book_size(book: dict) -> str:
    """尽量从搜索结果里拿到可读的文件大小。"""
    for key in ("filesizeString", "filesizeStr"):
        v = book.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    v = book.get("filesize", book.get("size"))
    if isinstance(v, (int, float)):
        return _human_size(v)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return ""


def _safe_filename(title: str, author: str, ext: str) -> str:
    def clean(s: str) -> str:
        s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s)
        return re.sub(r"\s+", " ", s).strip(" .")

    title = clean(title) or "book"
    author = clean(author)
    name = f"{title} - {author}" if author else title
    ext = clean(ext).lstrip(".").lower() or "pdf"
    return f"{name[:100]}.{ext}"


async def _resolve(host: str) -> list[str]:
    """解析域名，返回去重后的 IP 列表。"""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout=8
        )
        return sorted({info[4][0] for info in infos})
    except Exception:
        return []


class ZLibraryError(Exception):
    """Z-Library 相关操作异常"""


class DomainUnreachable(ZLibraryError):
    """站点不可达：域名失效、被墙、DNS 污染、代理没开，或被浏览器验证拦截"""


class ZLibraryClient:
    """Z-Library 网页版 eapi 客户端（登录 / 搜索 / 取下载链接 / 下载）

    base_url 可以填多个域名（逗号分隔），登录时会依次尝试，
    第一个能正常返回接口数据的域名会被固定使用；之后若该域名失效会自动切换到下一个。
    """

    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        base_url,
        email: str,
        password: str,
        proxy: str = "",
        timeout: float = 30,
        impersonate: str = "chrome",
    ):
        self.domains = _parse_domains(base_url)
        if not self.domains:
            raise ZLibraryError(
                "请在插件配置中填写 Z-Library 站点域名 base_url（例如 https://z-library.ec）"
            )
        self._order = list(self.domains)
        self.active: str | None = None
        self.email = (email or "").strip()
        self.password = password or ""
        self.proxy = (proxy or "").strip() or None
        self.timeout_s = float(timeout or 30)
        self.impersonate = (impersonate or "chrome").strip()
        self._session = None
        self._cookies: dict[str, str] = {}
        self._login_lock = asyncio.Lock()

    @property
    def engine(self) -> str:
        return f"curl_cffi（{self.impersonate} 指纹）" if _HAS_CFFI else "aiohttp"

    async def close(self):
        if self._session is None:
            return
        try:
            if _HAS_CFFI:
                await self._session.close()
            elif not self._session.closed:
                await self._session.close()
        except Exception:
            pass
        self._session = None

    async def _get_session(self):
        if self._session is None:
            if _HAS_CFFI:
                self._session = _CffiSession(
                    impersonate=self.impersonate, timeout=self.timeout_s
                )
            else:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_s)
                )
        return self._session

    def _headers(self, referer: str | None = None) -> dict:
        return {
            "User-Agent": self.UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": referer or f"{self.active or self._order[0]}/",
        }

    @property
    def base(self) -> str:
        if not self.active:
            raise ZLibraryError("尚未确定可用站点，请先登录")
        return self.active

    async def _fetch(
        self,
        method: str,
        url: str,
        *,
        data=None,
        headers: dict | None = None,
        cookies: dict | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """发请求，返回 (状态码, 文本, Content-Type)。网络错误统一转成 DomainUnreachable。

        代理或站点偶尔会掐断闲置后的第一条连接（curl 35 SSL_ERROR_SYSCALL 等），
        表现为「第一次找书报错、紧接着再试就好」。所以连接类失败会换一条新连接
        （重建 session）自动重试一次，仍失败才上报。
        """
        to = float(timeout or self.timeout_s)
        last_exc: Exception | None = None
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(1.0)
            session = await self._get_session()
            try:
                if _HAS_CFFI:
                    resp = await session.request(
                        method,
                        url,
                        data=data,
                        headers=headers,
                        cookies=cookies,
                        proxy=self.proxy,
                        timeout=to,
                        allow_redirects=True,
                    )
                    return (
                        resp.status_code,
                        resp.text or "",
                        resp.headers.get("Content-Type", ""),
                    )
                async with session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    cookies=cookies,
                    proxy=self.proxy,
                    timeout=aiohttp.ClientTimeout(total=to),
                ) as resp:
                    return (
                        resp.status,
                        await resp.text(),
                        resp.headers.get("Content-Type", ""),
                    )
            except ZLibraryError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:
                last_exc = e
                # 连接可能已经死在连接池里：丢弃整个 session，重试时新建
                try:
                    await self.close()
                except Exception:
                    pass
        msg = f"连接 {_host(url)} 失败：{last_exc}"
        low = str(last_exc or "").lower()
        if "timeout" in low or "timed out" in low:
            msg = f"连接 {_host(url)} 超时（已自动重试 1 次）"
        elif any(m in low for m in ("ssl", "reset", "closed", "proxy", "curl: (")):
            msg += "（通常是代理瞬断或闲置连接被掐断，已自动换新连接重试仍未成功；"
            msg += "请确认代理软件正在运行、节点可用）"
        raise DomainUnreachable(msg) from last_exc

    async def _request_json(self, method: str, url: str, **kwargs) -> dict:
        status, text, ctype = await self._fetch(method, url, **kwargs)
        body = (text or "").strip()
        if body.startswith("{") or "json" in (ctype or "").lower():
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        if _looks_like_challenge(body):
            raise DomainUnreachable(f"{_host(url)} {CHALLENGE_HINT}")
        if status >= 400:
            raise DomainUnreachable(f"{_host(url)} 返回 HTTP {status}")
        raise DomainUnreachable(f"{_host(url)} 返回的不是接口数据（HTTP {status}）")

    async def ensure_login(self):
        if self._cookies and self.active:
            return
        async with self._login_lock:
            if self._cookies and self.active:
                return
            if not self.email or not self.password:
                raise ZLibraryError(
                    "还没配置 Z-Library 账号：打开插件配置，填写 email（账号邮箱）和 "
                    "password（账号密码），其余项保持默认即可"
                )
            problems = []
            for domain in list(self._order):
                try:
                    data = await self._request_json(
                        "POST",
                        f"{domain}/eapi/user/login",
                        data={"email": self.email, "password": self.password},
                        headers=self._headers(domain),
                    )
                except DomainUnreachable as e:
                    problems.append(str(e))
                    continue
                user = (data or {}).get("user") or {}
                uid, key = user.get("id"), user.get("remix_userkey")
                if uid and key:
                    self._cookies = {
                        "remix_userid": str(uid),
                        "remix_userkey": str(key),
                    }
                    self.active = domain
                    self._promote(domain)
                    logger.info(f"[{PLUGIN_NAME}] Z-Library 登录成功：{domain}")
                    return
                text = str((data or {}).get("error") or data)
                if "password" in text.lower() or "email" in text.lower():
                    raise ZLibraryError(
                        f"Z-Library 账号或密码不正确（{domain} 返回：{text}）"
                    )
                problems.append(f"{domain}: {text}")
            raise DomainUnreachable("所有候选站点都无法登录。" + "；".join(problems))

    def _promote(self, domain: str):
        self._order = [domain] + [d for d in self._order if d != domain]

    def _failover(self):
        """当前域名失效：清掉登录态，把它挪到队尾，下次登录重新探测。"""
        if self.active:
            self._order = [d for d in self._order if d != self.active] + [self.active]
        self.active = None
        self._cookies = {}

    async def _with_failover(self, fn, *args):
        last: Exception | None = None
        for attempt in range(2):
            try:
                # ensure_login 也在 try 里：登录请求本身遇到代理瞬断时，
                # 换一轮重新登录（_fetch 内部还有一层「换新连接重试」）
                await self.ensure_login()
                return await fn(*args)
            except DomainUnreachable as e:
                last = e
                self._failover()
                if attempt == 0:
                    await asyncio.sleep(1.0)
        raise last or ZLibraryError("操作失败")

    async def search(
        self,
        query: str,
        limit: int = 8,
        order: str = "bestmatch",
        extensions=None,
        languages=None,
    ) -> list[dict]:
        async def _do():
            form = [
                ("message", query),
                ("order", order or "bestmatch"),
                ("page", "1"),
                ("limit", str(max(1, min(int(limit or 8), 50)))),
                ("yearFrom", ""),
                ("yearTo", ""),
            ]
            for ext in extensions or []:
                if str(ext).strip():
                    form.append(("extensions[]", str(ext).strip()))
            for lang in languages or []:
                if str(lang).strip():
                    form.append(("languages[]", str(lang).strip()))
            data = await self._request_json(
                "POST",
                f"{self.base}/eapi/book/search",
                data=form,
                headers=self._headers(),
                cookies=self._cookies,
            )
            if data.get("error"):
                raise ZLibraryError(f"搜索失败：{data.get('error')}")
            books = data.get("books") or data.get("results") or []
            return [b for b in books if isinstance(b, dict)]

        return await self._with_failover(_do)

    async def get_download_info(self, book: dict) -> tuple[dict, str]:
        book_id, book_hash = book.get("id"), book.get("hash")
        if not book_id or not book_hash:
            raise ZLibraryError("该结果缺少 id/hash 信息，无法下载，请重新搜索")

        async def _do():
            data = await self._request_json(
                "GET",
                f"{self.base}/eapi/book/{book_id}/{book_hash}/file",
                headers=self._headers(),
                cookies=self._cookies,
            )
            info = (data or {}).get("file") or {}
            link = (
                info.get("downloadLink") or info.get("downloadlink") or info.get("url")
            )
            if data.get("success") != 1 or not link:
                raise ZLibraryError(
                    f"获取下载链接失败：{data.get('error')}"
                    "（可能是今日下载额度已用完）"
                )
            return info, link

        return await self._with_failover(_do)

    async def download(
        self, link: str, dest_dir: Path, filename: str, timeout: float
    ) -> Path:
        session = await self._get_session()
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / filename
        tmp = path.with_suffix(path.suffix + ".part")
        to = float(timeout or 180)
        try:
            if _HAS_CFFI:
                resp = await session.get(
                    link,
                    headers=self._headers(),
                    proxy=self.proxy,
                    timeout=to,
                    stream=True,
                    allow_redirects=True,
                )
                try:
                    if resp.status_code != 200:
                        raise ZLibraryError(f"下载失败：HTTP {resp.status_code}")
                    if "json" in (resp.headers.get("Content-Type") or "").lower():
                        try:
                            data = json.loads(resp.text or "{}")
                        except ValueError:
                            data = {}
                        err = (data or {}).get("error") or data
                        raise ZLibraryError(
                            f"下载被拒绝：{err}（可能是今日下载额度已用完）"
                        )
                    with open(tmp, "wb") as fp:
                        async for chunk in resp.aiter_content():
                            fp.write(chunk)
                finally:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
            else:
                async with session.get(
                    link,
                    headers=self._headers(),
                    proxy=self.proxy,
                    timeout=aiohttp.ClientTimeout(total=to),
                ) as resp:
                    if resp.status != 200:
                        raise ZLibraryError(f"下载失败：HTTP {resp.status}")
                    if "json" in (resp.headers.get("Content-Type") or "").lower():
                        try:
                            data = json.loads(await resp.text())
                        except ValueError:
                            data = {}
                        err = (data or {}).get("error") or data
                        raise ZLibraryError(
                            f"下载被拒绝：{err}（可能是今日下载额度已用完）"
                        )
                    with open(tmp, "wb") as fp:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            fp.write(chunk)
            tmp.replace(path)
            return path
        except ZLibraryError:
            tmp.unlink(missing_ok=True)
            raise
        except aiohttp.ClientError as e:
            tmp.unlink(missing_ok=True)
            raise ZLibraryError(f"下载出错：{e}") from e
        except asyncio.TimeoutError as e:
            tmp.unlink(missing_ok=True)
            raise ZLibraryError("下载超时，可在插件配置调大 download_timeout") from e
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise ZLibraryError(f"下载出错：{e}") from e

    async def daily_quota(self) -> str | None:
        """返回今日已用/总下载额度，拿不到就返回 None。"""
        if not self._cookies or not self.active:
            return None
        try:
            data = await self._request_json(
                "GET",
                f"{self.base}/eapi/user/profile",
                headers=self._headers(),
                cookies=self._cookies,
            )
            user = (data or {}).get("user") or {}
            today = user.get("downloads_today")
            limit = user.get("downloads_limit") or user.get("dailyLimit")
            if today is not None and limit:
                return f"{today}/{limit}"
        except Exception:
            pass
        return None

    async def probe(self, domain: str) -> dict:
        """检查单个域名的 DNS 与接口可达性（给「找书检测」用）。

        注意：有些站点会拦首页但对 /eapi/* 放行，所以这里探测的是接口路径。
        """
        info = {
            "domain": domain,
            "dns": await _resolve(_host(domain)),
            "http": None,
            "note": "",
            "error": "",
        }
        try:
            status, text, _ = await self._fetch(
                "GET",
                f"{domain}/eapi/user/profile",
                headers={
                    "User-Agent": self.UA,
                    "Accept": "application/json",
                    "Referer": f"{domain}/",
                },
                cookies=self._cookies or None,
                timeout=15,
            )
        except Exception as e:
            info["error"] = str(e) or type(e).__name__
            return info
        info["http"] = status
        body = (text or "").strip()
        if body.startswith("{"):
            info["note"] = "接口正常"
        elif _looks_like_challenge(body):
            info["note"] = "浏览器验证"
        else:
            info["note"] = "非接口响应"
        return info


class ZLibraryFinder(Star):
    """Z-Library 找书插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._client: ZLibraryClient | None = None
        self._client_key: tuple | None = None
        self._results: dict[str, tuple[float, list[dict]]] = {}
        self._covers: dict[str, tuple[float, str]] = {}
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            data_dir = Path("data") / "plugin_data" / PLUGIN_NAME
        self._download_dir = Path(data_dir) / "downloads"
        self._download_dir.mkdir(parents=True, exist_ok=True)
        if not str(self.config.get("email") or "").strip() or not str(
            self.config.get("password") or ""
        ):
            logger.warning(
                f"[{PLUGIN_NAME}] 还没配置 Z-Library 账号：请在插件配置里填写 email 与 password。"
                "填好后可以发送「找书检测」确认连通性。"
            )

    # ---------- 内部工具 ----------

    def _get_client(self) -> ZLibraryClient:
        key = (
            str(self.config.get("base_url", "")),
            str(self.config.get("email", "")),
            str(self.config.get("password", "")),
            str(self.config.get("proxy", "")),
            str(self.config.get("impersonate", "")),
        )
        if self._client is None or self._client_key != key:
            old = self._client
            if old is not None:
                asyncio.ensure_future(old.close())
            self._client = ZLibraryClient(
                base_url=key[0],
                email=key[1],
                password=key[2],
                proxy=key[3],
                timeout=30,
                impersonate=key[4] or "chrome",
            )
            self._client_key = key
        return self._client

    def _save_results(self, key: str, books: list[dict]):
        self._gc_results()
        self._results[key] = (time.time(), books)

    def _get_results(self, key: str) -> list[dict]:
        item = self._results.get(key)
        if not item:
            return []
        ts, books = item
        if time.time() - ts > RESULT_TTL:
            self._results.pop(key, None)
            return []
        return books

    def _gc_results(self):
        now = time.time()
        expired = [k for k, (ts, _) in self._results.items() if now - ts > RESULT_TTL]
        for k in expired:
            self._results.pop(k, None)

    def _cleanup_downloads(self):
        try:
            now = time.time()
            for p in self._download_dir.iterdir():
                try:
                    if not p.is_file():
                        continue
                    if now - p.stat().st_mtime > DOWNLOAD_KEEP_HOURS * 3600:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def _full_query(self, event: AstrMessageEvent, arg) -> str:
        """取出完整查询词。

        新版 AstrBot 用 GreedyStr 会把剩余文本一次性传进来；老版本只能拿到第一个词，
        这时从原始消息里把「找书」后面的整段文本找回来，保证「找书 数据结构与算法 (C++版)」可用。
        """
        arg = str(arg or "").strip()
        raw = str(getattr(event, "message_str", "") or "").strip()
        if not raw:
            return arg
        match = re.search(r"(?:^|[\s/])(?:找书|zhaoshu)\s+(.+)$", raw, re.I | re.S)
        if match:
            full = re.sub(r"\s+", " ", match.group(1)).strip()
            if len(full) > len(arg):
                return full
        return arg

    def _troubleshoot(self) -> str:
        """排查建议；代理没配时额外点名。"""
        text = TROUBLESHOOT
        if not str(self.config.get("proxy") or "").strip():
            text += (
                "\n5) 你还没有设置 proxy。国内网络访问 Z-Library 会因 DNS 污染连不上，"
                "请在插件配置 proxy 里填代理，例如 http://127.0.0.1:7897"
            )
        return text

    async def _search_books(self, query: str) -> list[dict]:
        """按配置里的数量/排序/格式/语言执行一次搜索。"""
        client = self._get_client()
        limit = max(1, min(int(self.config.get("search_limit") or 8), 50))
        books = await client.search(
            query,
            limit=limit,
            order=str(self.config.get("order") or "bestmatch"),
            extensions=self.config.get("extensions"),
            languages=self.config.get("languages"),
        )
        return books[:limit]

    @staticmethod
    def _format_results(books: list[dict]) -> str:
        lines = [
            f"📖 在 Z-Library 找到 {len(books)} 本相关书籍，回复「选书 编号」下载：",
            "",
        ]
        for i, b in enumerate(books, 1):
            title = str(b.get("title") or "未知书名").strip()
            author = str(b.get("author") or "未知作者").strip()
            year = str(b.get("year") or "").strip()
            ext = str(b.get("extension") or b.get("format") or "").strip().upper()
            size = _book_size(b)
            lang = str(b.get("language") or "").strip()
            meta = " / ".join(x for x in (ext, size, lang, year) if x)
            line = f"{i}. 《{title}》 — {author}"
            if meta:
                line += f"（{meta}）"
            lines.append(line)
        return "\n".join(lines)

    def _remember_cover(self, key: str, b64: str):
        now = time.time()
        ttl = max(1, int(self.config.get("cover_ttl_minutes") or 10)) * 60
        for k, (ts, _) in list(self._covers.items()):
            if now - ts > ttl:
                self._covers.pop(k, None)
        self._covers[key] = (now, b64)

    async def _pick_cover(self, event: AstrMessageEvent) -> str:
        """优先用本条消息里的图片，其次用本会话最近发过的图片。"""
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for seg in message:
            if not isinstance(seg, Comp.Image):
                continue
            try:
                b64 = await seg.convert_to_base64()
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 图片转码失败: {e}")
                continue
            if b64:
                return b64
        key = event.unified_msg_origin
        item = self._covers.get(key)
        if not item:
            return ""
        ts, b64 = item
        ttl = max(1, int(self.config.get("cover_ttl_minutes") or 10)) * 60
        if time.time() - ts > ttl:
            self._covers.pop(key, None)
            return ""
        return b64

    async def _send_book_file(
        self, event: AstrMessageEvent, path: Path, name: str
    ) -> tuple[bool, str]:
        """把文件发给当前会话。

        AstrBot 默认把 File 消息段塞进 send_private_msg，部分协议端（如 SnowLuma）
        会以 "send c2c file message rejected" 拒绝。这里改成直接调 OneBot 的
        upload_private_file / upload_group_file：先上传拿 file_id，再发文件消息。
        返回 (是否成功, 错误信息)；平台没有 call_action 时返回 UNSUPPORTED。
        """
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        if not callable(call):
            return False, UNSUPPORTED
        target = str(path.resolve())
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        try:
            if group_id:
                await call(
                    "upload_group_file",
                    group_id=int(group_id),
                    file=target,
                    name=name,
                )
            else:
                await call(
                    "upload_private_file",
                    user_id=int(event.get_sender_id()),
                    file=target,
                    name=name,
                )
            return True, ""
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 发送文件失败: {e}")
            return False, str(e)[:160]

    def _vision_provider_ids(self) -> list[str]:
        raw = str(self.config.get("vision_provider_id") or "")
        return [x for x in re.split(r"[,，\s]+", raw.strip()) if x]

    async def _recognize_cover(
        self, event: AstrMessageEvent, b64: str
    ) -> tuple[str, str, list[str]]:
        """按顺序尝试视觉模型识别封面，返回 (书名, 作者, 失败原因列表)。"""
        prompt = (
            str(self.config.get("vision_prompt") or "").strip() or DEFAULT_VISION_PROMPT
        )
        failures: list[str] = []
        candidates: list[tuple[str, object]] = []
        ids = self._vision_provider_ids()
        if ids:
            for pid in ids:
                try:
                    prov = self.context.get_provider_by_id(pid)
                except Exception as e:
                    prov = None
                    logger.warning(f"[{PLUGIN_NAME}] 获取模型 {pid} 失败: {e}")
                if prov is None:
                    failures.append(
                        f"{pid}: 未找到该模型 —— 这个 ID 必须是 AstrBot「服务提供商」里已配置的，"
                        "留空则用当前默认模型"
                    )
                else:
                    candidates.append((pid, prov))
        else:
            try:
                prov = self.context.get_using_provider(event.unified_msg_origin)
            except Exception as e:
                prov = None
                logger.warning(f"[{PLUGIN_NAME}] 获取默认模型失败: {e}")
            if prov is None:
                failures.append(
                    "没有可用的对话模型 —— 先在 AstrBot「服务提供商」里配置一个支持图片的模型，"
                    "或在插件配置里关闭 cover_search（搜索和下载不需要模型）"
                )
            else:
                candidates.append(("当前默认模型", prov))
        if not candidates:
            return "", "", failures

        for name, prov in candidates:
            try:
                resp = await prov.text_chat(
                    prompt=prompt, image_urls=[f"base64://{b64}"]
                )
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] {name} 识别失败: {e}")
                failures.append(f"{name}: {_explain_llm_error(e)}")
                continue
            answer = (getattr(resp, "completion_text", "") or "").strip()
            if not answer:
                # 推理模型可能把内容放在 reasoning_content 里
                answer = (getattr(resp, "reasoning_content", "") or "").strip()
            title, author = _parse_book_guess(answer)
            if title:
                return title, author, failures
            failures.append(f"{name}: 没能从回复里读出书名")
        return "", "", failures

    # ---------- 指令 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_image(self, event: AstrMessageEvent):
        """记下会话里最近一张图片，供「以图找书」使用。"""

        if not bool(self.config.get("cover_search", True)):
            return
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for seg in message:
            if not isinstance(seg, Comp.Image):
                continue
            try:
                b64 = await seg.convert_to_base64()
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 图片转码失败: {e}")
                return
            if b64:
                self._remember_cover(event.unified_msg_origin, b64)
            return

    @filter.command("找书", alias={"zhaoshu"})
    async def find_book(self, event: AstrMessageEvent, query: GreedyStr):
        """找书 <书名/作者/关键词>：在 Z-Library 搜索书籍"""
        query = self._full_query(event, query)
        if not query:
            yield event.plain_result("用法：找书 <书名或关键词>，例如：找书 三体")
            return
        yield event.plain_result(f"🔎 正在 Z-Library 搜索「{query}」…")
        try:
            books = await self._search_books(query)
        except DomainUnreachable as e:
            yield event.plain_result(f"❌ 搜索失败：{e}{self._troubleshoot()}")
            return
        except ZLibraryError as e:
            yield event.plain_result(f"❌ 搜索失败：{e}")
            return
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 搜索异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 搜索出错：{e}")
            return
        if not books:
            yield event.plain_result(
                f"没有找到与「{query}」相关的书籍，可以换个关键词再试。"
            )
            return
        self._save_results(event.unified_msg_origin, books)
        yield event.plain_result(self._format_results(books))

    @filter.command("以图找书", alias={"找书封面", "coverbook"})
    async def find_by_cover(self, event: AstrMessageEvent):
        """以图找书：发送书籍封面图片，自动识别书名后搜索"""
        if not bool(self.config.get("cover_search", True)):
            yield event.plain_result(
                "封面找书已被关闭，可在插件配置里打开 cover_search。"
            )
            return
        b64 = await self._pick_cover(event)
        if not b64:
            yield event.plain_result(
                "把书的封面图发给我就行：可以先发图片再发「以图找书」，"
                "也可以图片和命令放在同一条消息里（默认 10 分钟内有效）。"
            )
            return
        yield event.plain_result("🔍 正在识别封面上的书名…")
        title, author, failures = await self._recognize_cover(event, b64)
        if not title:
            lines = ["❌ 封面识别失败："]
            lines.extend(f"• {f}" for f in failures)
            lines.append(
                "\n可以在 vision_provider_id 里按顺序填多个模型（逗号分隔），"
                "插件会依次尝试；也可以稍后再试。"
            )
            yield event.plain_result("\n".join(lines))
            return
        if not title:
            yield event.plain_result(
                "没能从这张图里读出书名。可以换成封面主体更清晰、文字更完整的图片，"
                "或直接发「找书 书名」。"
            )
            return

        shown = f"《{title}》" + (f" — {author}" if author else "")
        yield event.plain_result(f"📚 识别为 {shown}，正在搜索…")
        try:
            books = await self._search_books(f"{title} {author}".strip())
            if not books and author:
                books = await self._search_books(title)
        except DomainUnreachable as e:
            yield event.plain_result(f"❌ 搜索失败：{e}{self._troubleshoot()}")
            return
        except ZLibraryError as e:
            yield event.plain_result(f"❌ 搜索失败：{e}")
            return
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 搜索异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 搜索出错：{e}")
            return
        if not books:
            yield event.plain_result(
                f"识别为 {shown}，但 Z-Library 没搜到。可以试试「找书 {title}」换个关键词。"
            )
            return
        self._save_results(event.unified_msg_origin, books)
        yield event.plain_result(f"{shown} 的搜索结果：\n\n" + self._format_results(books))

    @filter.command("选书", alias={"xuanshu"})
    async def pick_book(self, event: AstrMessageEvent, index: str = ""):
        """选书 <编号>：下载找书结果中的某本书并把文件发给找书人"""
        key = event.unified_msg_origin
        books = self._get_results(key)
        if not books:
            yield event.plain_result(
                "当前会话没有可用的搜索结果（30 分钟内有效），请先使用「找书 <关键词>」。"
            )
            return
        idx = str(index or "").strip()
        if not idx.isdigit() or not 1 <= int(idx) <= len(books):
            yield event.plain_result(f"用法：选书 <编号>（当前可选 1-{len(books)}）")
            return

        book = books[int(idx) - 1]
        title = str(book.get("title") or "未知书名").strip()
        author = str(book.get("author") or "").strip()
        ext = str(book.get("extension") or "pdf").strip().lower()
        client = self._get_client()

        link = None
        try:
            yield event.plain_result(f"📥 正在获取《{title}》的下载链接…")
            info, link = await client.get_download_info(book)
            size_bytes = book.get("filesize")
            size_bytes = size_bytes if isinstance(size_bytes, (int, float)) else 0
            max_mb = int(self.config.get("max_file_mb") or 0)
            too_big = max_mb > 0 and size_bytes > max_mb * 1024 * 1024
            if not bool(self.config.get("send_file", True)) or too_big:
                reason = (
                    "文件超过了 max_file_mb 限制" if too_big else "插件配置为不直接发送文件"
                )
                yield event.plain_result(
                    f"📄 {reason}，请通过链接下载《{title}》：\n{link}"
                )
                return
            yield event.plain_result(
                f"⏬ 正在下载《{title}》并发送给找书人，书籍较大时可能需要等待一会儿…"
            )
            filename = _safe_filename(title, author, ext)
            path = await client.download(
                link,
                self._download_dir,
                filename,
                timeout=int(self.config.get("download_timeout") or 180),
            )
            self._cleanup_downloads()
            quota = await client.daily_quota()
            tip = f"\n今日下载额度：{quota}" if quota else ""

            sent, err = await self._send_book_file(event, path, filename)
            if sent:
                yield event.plain_result(f"✅ 《{title}》已发送{tip}")
                return

            # 协议端不支持 upload_*_file 时，退回框架的文件消息段（Telegram 等平台走这条）
            if err == UNSUPPORTED:
                yield event.chain_result(
                    [Comp.File(name=filename, file=str(path.resolve()))]
                )
                yield event.plain_result(f"✅ 《{title}》已发送{tip}")
                return
            yield event.plain_result(
                f"⚠️ 《{title}》已下载，但发送文件失败：{err}\n"
                f"文件在服务器上：{path}\n"
                "如果协议端和 AstrBot 不在同一台机器，请在 AstrBot 配置里设置 "
                "callback_api_base，让它改发临时链接。" + tip
            )
        except DomainUnreachable as e:
            msg = f"❌ 下载失败：{e}"
            if link:
                msg += f"\n也可以试试直接打开链接：{link}"
            yield event.plain_result(msg + self._troubleshoot())
        except ZLibraryError as e:
            msg = f"❌ 下载失败：{e}"
            if link:
                msg += (
                    "\n也可以试试直接打开链接（部分链接需要登录后才能访问）：" f"\n{link}"
                )
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 下载异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 下载出错：{e}")

    @filter.command("找书检测", alias={"zlibcheck"})
    async def check(self, event: AstrMessageEvent):
        """找书检测：检查站点 DNS、连通性与登录状态"""
        yield event.plain_result("🩺 正在检测 Z-Library 连接情况…")
        try:
            client = self._get_client()
        except ZLibraryError as e:
            yield event.plain_result(f"❌ 配置有误：{e}")
            return

        vision_ids = self._vision_provider_ids()
        lines = [
            "配置体检：",
            f"  引擎：{client.engine}",
            f"  账号：{client.email or '未填写 —— 必填，填 Z-Library 账号邮箱'}",
            f"  密码：{'已填写' if client.password else '未填写 —— 必填'}",
            f"  代理：{client.proxy or '未设置（国内网络需要，如 http://127.0.0.1:7897）'}",
            f"  视觉模型：{'、'.join(vision_ids) if vision_ids else '跟随当前默认模型'}",
            "",
            "站点探测：",
        ]
        for domain in client.domains:
            item = await client.probe(domain)
            dns = ", ".join(item["dns"]) or "解析失败"
            if item["http"] is None:
                state = f"不可达（{item['error'][:60]}）"
            elif item["note"] == "接口正常":
                state = "接口正常"
            elif item["note"]:
                state = f"HTTP {item['http']}（{item['note']}，接口不可直接调用）"
            else:
                state = f"HTTP {item['http']}"
            lines.append(f"• {item['domain']}\n  DNS: {dns}\n  连接: {state}")

        try:
            await client.ensure_login()
            lines += ["", f"✅ 登录成功，当前使用：{client.active}"]
            quota = await client.daily_quota()
            if quota:
                lines.append(f"今日下载额度：{quota}")
        except DomainUnreachable as e:
            lines += ["", f"❌ 登录失败：{e}", self._troubleshoot()]
        except ZLibraryError as e:
            lines += ["", f"❌ 登录失败：{e}"]
        except Exception as e:
            lines += ["", f"❌ 检测出错：{e}"]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        if self._client is not None:
            await self._client.close()

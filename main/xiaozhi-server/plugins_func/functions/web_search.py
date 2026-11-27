import httpx
import json
from urllib.parse import urlsplit
from config.logger import setup_logging
from plugins_func.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_DEFAULT_DESCRIPTION = (
    "联网搜索工具。当用户明确需要联网搜索问题时使用此工具。"
)

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题",
                }
            },
            "required": ["query"],
        },
    },
}


async def _search_metaso(api_key: str, query: str, max_results: int) -> str:
    """调用秘塔搜索API"""
    url = "https://metaso.cn/api/v1/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "size": max_results,
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    logger.bind(tag=TAG).debug(f"秘塔搜索请求 | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"秘塔搜索响应 | status: {response.status_code}")

    webpages = data.get("webpages", [])
    if not webpages:
        return "未找到相关搜索结果。"

    lines = ["【联网搜索结果】"]
    for i, item in enumerate(webpages, 1):
        title = item.get("title", "无标题")
        snippet = item.get("summary", "")
        date = item.get("date", "")
        lines.append(f"{i}. 标题：{title}")
        if date:
            lines.append(f"   日期：{date}")
        if snippet:
            lines.append(f"   摘要：{snippet}")

    return "\n".join(lines)


async def _search_tavily(api_key: str, query: str, max_results: int) -> str:
    """调用Tavily搜索API"""
    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": "advanced",
    }
    logger.bind(tag=TAG).debug(f"Tavily搜索请求 | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Tavily搜索响应 | status: {response.status_code} | data: {data}")

    results = data.get("results", [])
    if not results:
        return "未找到相关搜索结果。"

    answer = data.get("answer", "")
    lines = [f"【联网搜索结果】\n总结：{answer}"]
    # for i, item in enumerate(results, 1):
    #     title = item.get("title", "无标题")
    #     summary = item.get("content", "")
    #     lines.append(f"{i}. 标题：{title}")
    #     if summary:
    #         lines.append(f"   摘要：{summary}")

    return "\n".join(lines)


async def _search_serply(api_key: str, query: str, max_results: int) -> str:
    """调用Serply搜索API"""
    url = "https://api.serply.io/v1/search"
    headers = {
        "X-Api-Key": api_key,
        "User-Agent": "xiaozhi-esp32-server",
    }
    # Serply单页最多返回10条，且返回条数可能略多于num，需要本地再截断一次
    params = {"q": query, "num": min(max_results, 10)}
    logger.bind(tag=TAG).debug(f"Serply搜索请求 | URL: {url} | params: {params}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.get(url, params=params, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Serply搜索响应 | status: {response.status_code}")

    if response.status_code != 200:
        detail = data.get("detail", "") if isinstance(data, dict) else ""
        logger.bind(tag=TAG).error(
            f"Serply搜索失败 | status: {response.status_code} | detail: {detail}"
        )
        return "联网搜索请求失败，请检查API Key是否正确。"

    results = data.get("results", [])
    if not results:
        return "未找到相关搜索结果。"

    lines = ["【联网搜索结果】"]
    for i, item in enumerate(results[:max_results], 1):
        title = item.get("title", "无标题")
        snippet = item.get("description", "")
        lines.append(f"{i}. 标题：{title}")
        if snippet:
            lines.append(f"   摘要：{snippet}")

    return "\n".join(lines)


@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    logger.bind(tag=TAG).info(f"web_search 被调用 | query={query}")
    if not query:
        return ActionResponse(Action.REQLLM, "请提供搜索关键词。", None)

    web_search_config = conn.config.get("plugins", {}).get("web_search", {})
    provider = web_search_config.get("provider", "").lower()
    max_results = int(web_search_config.get("max_results", 3))
    logger.bind(tag=TAG).info(f"web_search 配置 | provider={provider} | max_results={max_results} | config_keys={list(web_search_config.keys())}")

    api_key = web_search_config.get("api_key", "")
    if not api_key:
        return ActionResponse(
            Action.REQLLM,
            "联网搜索功能未配置API Key，请在配置文件中填写。",
            None,
        )

    try:
        if provider == "metaso":
            result_text = await _search_metaso(api_key, query, max_results)
        elif provider == "tavily":
            result_text = await _search_tavily(api_key, query, max_results)
        elif provider == "serply":
            result_text = await _search_serply(api_key, query, max_results)
        else:
            return ActionResponse(
                Action.REQLLM,
                f"联网搜索功能未配置或配置的搜索源无效（当前：{provider}），请检查配置。",
                None,
            )
        logger.bind(tag=TAG).info(f"搜索结果组装完成:\n{result_text}")
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("联网搜索请求超时")
        result_text = "联网搜索请求超时，请稍后重试。"
    except httpx.HTTPStatusError as e:
        logger.bind(tag=TAG).error(f"联网搜索请求失败: {e}")
        result_text = "联网搜索请求失败，请稍后重试。"
    except Exception as e:
        logger.bind(tag=TAG).error(f"联网搜索异常: {e}")
        result_text = "联网搜索出现异常，请稍后重试。"

    return ActionResponse(Action.REQLLM, result_text, None)


async def search_evidence(conn, query):
    """Structured read-only search. Preserve provider-returned URLs, never invent them.

    Reuses existing search provider settings; DashScope is available with the
    explicitly configured independent intent model when no search key is bound.
    """
    settings = (conn.config.get("plugins") or {}).get("web_search") or {}
    if isinstance(settings,str):
        try:settings=json.loads(settings)
        except ValueError:settings={}
    if not isinstance(settings,dict):settings={}
    provider = settings.get("provider", "dashscope").lower()
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3), trust_env=False) as client:
        if provider == "dashscope":
            llm = conn.intent.llm
            base = urlsplit(llm.base_url)
            if not base.hostname or not base.hostname.endswith(".aliyuncs.com"):
                raise ValueError("Unsupported search endpoint")
            url = base.scheme + "://" + base.netloc + "/api/v1/services/aigc/multimodal-generation/generation"
            payload = {"model": llm.model_name,
                       "input": {"messages": [{"role": "user", "content": [{"text": query}]}]},
                       "parameters": {"enable_thinking": False, "enable_search": True,
                                      "search_options": {"forced_search": True, "enable_source": True,
                                                         "prepend_search_result": True},
                                      "max_tokens": 128, "incremental_output": True}}
            sources = []
            async with client.stream("POST", url, json=payload, headers={
                "Authorization": "Bearer " + llm.api_key, "X-DashScope-SSE": "enable"}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    if event.get("code"):
                        raise ValueError("Search provider error")
                    sources.extend(event.get("output", {}).get("search_info", {}).get("search_results", []) or [])
                    if sources:
                        # Sources are prepended; an unused generated answer adds latency.
                        break
            rows = [{"title": s.get("title", ""), "url": s.get("url", ""),
                     "publisher": s.get("site_name", ""), "content": "", "published_at": None}
                    for s in sources]
        else:
            key = settings.get("api_key")
            if not key:
                raise ValueError("Search credentials unavailable")
            if provider == "metaso":
                response = await client.post("https://metaso.cn/api/v1/search",
                    headers={"Authorization": "Bearer " + key},
                    json={"q": query, "size": 5, "scope": "webpage", "includeSummary": True})
                response.raise_for_status()
                rows = [{"title": s.get("title", ""), "url": s.get("link") or s.get("url", ""),
                         "content": s.get("summary", ""), "publisher": s.get("siteName", ""),
                         "published_at": s.get("date")}
                        for s in response.json().get("webpages", [])]
            elif provider == "tavily":
                response = await client.post("https://api.tavily.com/search",
                    headers={"Authorization": "Bearer " + key},
                    json={"query": query, "max_results": 5, "search_depth": "basic",
                          "include_answer": False, "include_published_date": True})
                response.raise_for_status()
                rows = [{"title": s.get("title", ""), "url": s.get("url", ""),
                         "content": s.get("content", ""), "publisher": "",
                         "published_at": s.get("published_date")}
                        for s in response.json().get("results", [])]
            elif provider == "serply":
                response = await client.get("https://api.serply.io/v1/search",
                    headers={"X-Api-Key": key}, params={"q": query, "num": 5})
                response.raise_for_status()
                rows = [{"title": s.get("title", ""), "url": s.get("url") or s.get("link", ""),
                         "content": s.get("description", ""), "publisher": "", "published_at": None}
                        for s in response.json().get("results", [])]
            else:
                raise ValueError("Unknown search provider")
    unique = {}
    for row in rows:
        parsed = urlsplit(row["url"])
        if parsed.scheme in ("http", "https") and parsed.hostname:
            unique.setdefault(row["url"], row)
    return list(unique.values())[:8]

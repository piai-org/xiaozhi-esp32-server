"""Bounded information reads, using existing RSS, weather and search adapters."""
import asyncio
import json
from datetime import datetime, timedelta, date
from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import re
import socket
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

TZ = ZoneInfo("Asia/Shanghai")


def target_date(reference, now=None):
    today = (now or datetime.now(TZ)).date()
    shifts = {"now": 0, "today": 0, "tomorrow": 1, "day_after_tomorrow": 2, "yesterday": -1}
    if reference in shifts:
        return today + timedelta(days=shifts[reference])
    return date.fromisoformat(reference)


def dated(value):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def article_text(soup, hostname=''):
    """Keep the verified publisher's article body out of recommendation lists."""
    for node in soup(["script", "style", "nav", "footer", "header"]):
        node.decompose()
    body = soup.select_one('div.left_zw') if hostname.endswith('chinanews.com.cn') else None
    body = body or soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in body.find_all("p")]
    return "\n".join(p for p in paragraphs if len(p) >= 15)[:5500]


async def public_page(url, with_metadata=False):
    """Read only returned public URLs, without redirects to internal resources."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        return ""
    addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        return ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2), follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 400000:
                    break
                chunks.append(chunk)
            content = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    soup = BeautifulSoup(content, "html.parser")
    published = None
    for key in ("article:published_time", "pubdate", "publishdate", "date"):
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        parsed_date = dated(tag.get("content")) if tag else None
        if parsed_date:
            published = parsed_date.isoformat()
            break
    deferred = soup.select_one("textarea.article-content")
    if deferred:
        soup = BeautifulSoup(deferred.get_text(), "html.parser")
    text = article_text(soup, parsed.hostname)
    return {"content": text, "published_at": published} if with_metadata else text


class InformationLookup:
    def __init__(self, conn, session):
        self.conn, self.session = conn, session

    @staticmethod
    def failure_message(bundles):
        codes = {b.get("error_code") for b in bundles}
        if "location_required" in codes:
            return "您想查哪个城市的天气？"
        if "location_ambiguous" in codes:
            return "有几个同名的地方，您说的是哪个省、哪个市？"
        if "date_unsupported" in codes:
            return "这次没有查到您指定那一天的资料，咱们换个日期再试试？"
        if "no_previous_result" in codes:
            return "我这里没有刚才那条查询结果，您想查什么？"
        return "这次没能查到可靠的资料，您可以稍后再试。"

    async def execute(self, requests):
        # At most two independent reads; state commits only after all finish.
        bundles = await asyncio.gather(*(self.one(dict(r)) for r in requests))
        return bundles

    async def one(self, request):
        result = {"status": "error", "topic": request["topic"], "query": request["query"],
                  "operation": request["operation"],
                  "requested_result_limit": request["result_limit"],
                  "fetched_at": datetime.now(TZ).isoformat(), "results": [], "error_code": None}
        try:
            if request["operation"] in ("detail", "next"):
                rows = await self.previous(request)
            elif request["topic"] == "weather":
                rows = await self.weather(request)
            elif request["topic"] == "news":
                rows = await self.news(request)
            else:
                rows = await self.general(request)
            for row in rows:
                row.setdefault("source_id", hashlib.sha256(row["url"].encode()).hexdigest()[:16])
            result.update(status="ok" if rows else "empty", results=rows[:request["result_limit"]])
        except asyncio.CancelledError:
            raise
        except (ValueError, LookupError) as exc:
            result["error_code"] = str(exc) if str(exc) in {
                "location_required", "location_ambiguous", "date_unsupported", "no_previous_result"} else "invalid_or_unavailable"
        except (httpx.TimeoutException, asyncio.TimeoutError):
            result.update(status="timeout", error_code="timeout")
        except Exception as exc:
            result["error_code"] = type(exc).__name__
        return result

    def commit(self, requests, bundles):
        """Called by the owner only if this generation is still current."""
        for request, bundle in zip(requests, bundles):
            if bundle["status"] != "ok":
                continue
            self.session.results = bundle["results"]
            self.session.seen.update(row["url"] for row in bundle["results"])
            if request["operation"] == "detail":
                # A detail view must not renumber the original list.
                continue
            self.session.result_sets[request["topic"]] = bundle["results"]
            state = {"topic": request["topic"], "query": request["query"],
                "keywords": request.get("keywords", []),
                "location": request["location"], "time_reference": request["time_reference"],
                "results": [{"index": i, "title": row["title"], "source_id": row["source_id"]}
                            for i, row in enumerate(bundle["results"], 1)]}
            queries = self.session.state.get("queries", {})
            queries[request["topic"]] = dict(state)
            self.session.state = dict(state, queries=queries)

    async def previous(self, request):
        if request["topic"] == "weather":
            return await self.weather(request)
        if request["operation"] == "next":
            request["operation"] = "search"
            if request["topic"] == "news":
                return await self.news(request, exclude=self.session.seen)
            return [row for row in await self.general(request) if row["url"] not in self.session.seen]
        index = request["reference_index"] or 1
        previous = self.session.result_sets.get(request["topic"], [])
        if not 1 <= index <= len(previous):
            raise LookupError("no_previous_result")
        row = dict(previous[index - 1])
        try:
            body = await public_page(row["url"])
            if body:
                row["content"] = body
                row["content_scope"] = "page_excerpt"
        except (httpx.HTTPError, OSError, ValueError):
            pass
        return [row]

    async def weather(self, request):
        from plugins_func.functions.get_weather import fetch_city_info, fetch_weather_page, parse_weather_info
        location = request["location"]
        if not location:
            raise ValueError("location_required")
        day = target_date(request["time_reference"])
        today = datetime.now(TZ).date()
        if not today <= day <= today + timedelta(days=6):
            raise ValueError("date_unsupported")
        settings = (self.conn.config.get("plugins") or {}).get("get_weather") or {}
        if isinstance(settings,str):
            try:settings=json.loads(settings)
            except ValueError:settings={}
        if not isinstance(settings,dict):settings={}
        city = None
        if settings.get("api_key") and settings.get("api_host"):
            try:
                city = await fetch_city_info(location, settings["api_key"], settings["api_host"])
            except (httpx.HTTPError, ValueError):
                pass
        if not city or not city.get("fxLink"):
            return await self.open_meteo(location, day)
        # Explicit location only: never derive a city from the server/proxy IP.
        soup = await fetch_weather_page(city["fxLink"])
        if soup is None:
            return await self.open_meteo(location, day)
        city_name, current, basics, forecasts = parse_weather_info(soup)
        rows = []
        for label, condition, high, low in forecasts:
            normalized = label.strip()
            try:
                if re.fullmatch(r"\d{1,2}/\d{1,2}", normalized):
                    month, number = map(int, normalized.split("/"))
                    year = today.year + (1 if today.month == 12 and month == 1 else 0)
                    forecast_day = date(year, month, number)
                else:
                    forecast_day = date.fromisoformat(normalized)
            except ValueError:
                continue
            if forecast_day == day:
                rows.append({"date": day.isoformat(), "condition": condition, "high": high, "low": low})
        if day != today and not rows:
            return await self.open_meteo(location, day)
        data = {"location": city_name, "location_id": city.get("id"), "requested_date": day.isoformat(),
                "current": current if day == today else None,
                "current_details": basics if day == today else {}, "forecast": rows,
                "observed_at": None, "temperature_unit": "摄氏度",
                "data_note": "抓取时间不是观测时间；预报是预测，缺失字段未知"}
        return [{"title": city_name + "天气（" + day.isoformat() + "）", "url": city["fxLink"],
                 "publisher": "和风天气", "published_at": None,
                 "content": json.dumps(data, ensure_ascii=False), "weather": data}]

    async def open_meteo(self, location, day):
        """Keyless test fallback; geocoding and forecast remain real API reads.

        Open-Meteo free endpoint is for non-commercial evaluation. A commercial
        deployment must configure an appropriately licensed endpoint/provider.
        """
        name = location.removesuffix("市")
        async with httpx.AsyncClient(timeout=httpx.Timeout(7, connect=3), trust_env=False) as client:
            response = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                params={"name": name, "count": 10, "language": "zh", "format": "json"})
            response.raise_for_status()
            candidates = response.json().get("results", [])
            matches = [c for c in candidates if name in (c.get("name"), (c.get("admin2") or "").removesuffix("市"))]
            administrative = [c for c in matches if c.get("feature_code") in ("PPLC", "PPLA", "PPLA2")]
            matches = administrative or matches
            if len(matches) != 1:
                raise ValueError("location_ambiguous" if len(matches) > 1 else "location_required")
            city = matches[0]
            params = {"latitude": city["latitude"], "longitude": city["longitude"],
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "Asia/Shanghai", "forecast_days": 7}
            response = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            response.raise_for_status()
            raw = response.json()
            source_url = str(response.url)
        daily = raw.get("daily", {})
        if day.isoformat() not in daily.get("time", []):
            raise ValueError("date_unsupported")
        index = daily["time"].index(day.isoformat())
        def value(key):
            values = daily.get(key, [])
            return values[index] if len(values) > index else None
        conditions = {0:"晴",1:"大部晴朗",2:"局部多云",3:"阴",45:"雾",48:"雾凇",
            51:"小毛毛雨",53:"中毛毛雨",55:"大毛毛雨",56:"冻毛毛雨",57:"强冻毛毛雨",
            61:"小雨",63:"中雨",65:"大雨",66:"小冻雨",67:"强冻雨",71:"小雪",73:"中雪",75:"大雪",
            77:"雪粒",80:"小阵雨",81:"中阵雨",82:"强阵雨",85:"小阵雪",86:"强阵雪",
            95:"雷雨",96:"雷雨伴小冰雹",99:"雷雨伴大冰雹"}
        current = raw.get("current", {}) if day == datetime.now(TZ).date() else {}
        data = {"location": city["name"], "admin1": city.get("admin1"), "country": city.get("country"),
            "requested_date": day.isoformat(), "current_model_estimate": {
                "time": current.get("time"), "temperature": current.get("temperature_2m"),
                "humidity_percent": current.get("relative_humidity_2m"),
                "condition": conditions.get(current.get("weather_code")),
            } if current else None,
            "forecast": {"date": day.isoformat(), "condition": conditions.get(value("weather_code")),
                "high": value("temperature_2m_max"), "low": value("temperature_2m_min"),
                "precipitation_probability_percent": value("precipitation_probability_max")},
            "temperature_unit": "摄氏度", "timezone": "Asia/Shanghai",
            "data_note": "Open-Meteo数值模式估计与预报，不是本站实测；抓取时间不是观测时间；降水概率不是确定会下雨"}
        return [{"title": city["name"] + "天气（" + day.isoformat() + "）", "url": source_url,
            "publisher": "Open-Meteo", "published_at": None, "weather": data,
            "content": json.dumps(data, ensure_ascii=False)}]

    async def news(self, request, exclude=None):
        from plugins_func.functions.get_news_from_chinanews import fetch_news_from_rss
        query = request["query"]
        channels = ["china", "world", "society"]
        for word, channel in [("国际", "world"), ("国外", "world"), ("财经", "finance"), ("经济", "finance"),
                              ("社会", "society"), ("科技", "it"), ("体育", "sports")]:
            if word in query:
                channels = [channel]
                break
        groups = await asyncio.gather(*(fetch_news_from_rss("https://www.chinanews.com.cn/rss/" + c + ".xml") for c in channels))
        cutoff = target_date(request["time_reference"]) if request["time_reference"] != "week" else None
        now = datetime.now(TZ)
        rows = []
        seen = set(exclude or [])
        titles = set()
        for items in groups:
            for item in items:
                timestamp = dated(item.get("pubDate"))
                if timestamp is None or timestamp > now:
                    continue
                if cutoff and timestamp.date() != cutoff:
                    continue
                if cutoff is None and timestamp < now - timedelta(days=7):
                    continue
                url = item.get("link", "")
                title = item.get("title", "").strip()
                if not url.startswith("https://www.chinanews.com.cn/") or url in seen or title in titles:
                    continue
                seen.add(url); titles.add(title)
                content = BeautifulSoup(item.get("description", ""), "html.parser").get_text(" ", strip=True)
                if any(word not in title + content for word in request.get("keywords", [])):
                    continue
                rows.append({"title": title, "url": url, "publisher": "中国新闻网",
                             "published_at": timestamp.isoformat(), "content": content[:3000],
                             "content_scope": "rss_summary" if content else "title_only"})
        rows.sort(key=lambda row: row["published_at"], reverse=True)
        # This list is recent source coverage, not a claim to rank global importance.
        return rows[:request["result_limit"]]

    async def general(self, request):
        from plugins_func.functions.web_search import search_evidence
        rows = await search_evidence(self.conn, request["query"])
        async def enrich(row):
            if not row.get("content"):
                try:
                    fetched = await public_page(row["url"], with_metadata=True)
                    if isinstance(fetched, dict):
                        row.update(fetched)
                except (httpx.HTTPError, OSError, ValueError):
                    pass
            row["content"] = (row.get("content") or "")[:4500]
            row["content_scope"] = "source_excerpt" if row["content"] else "title_only"
            return row
        enriched = await asyncio.gather(*(enrich(dict(row)) for row in rows[:8]))
        # Prefer usable text; a search title alone cannot ground a knowledge answer.
        usable = [row for row in enriched if len(row.get("content", "")) >= 60]
        # A current-information query cannot be grounded in an undated old page.
        # This supplements routing; it is not a keyword switch for tool calling.
        if re.search("今天|今日|当前|现在|近期|最近|今年|最新|实时|目前", request["query"]):
            now = datetime.now(TZ)
            usable = [row for row in usable if dated(row.get("published_at")) is not None
                      and now - timedelta(days=7) <= dated(row["published_at"]) <= now]
        # Prefer institutional sources. A lone media/repost article is not enough
        # to validate a knowledge claim; two sites still do not guarantee truth.
        def institutional(row):
            host=urlsplit(row['url']).hostname or ''
            return any(host==suffix or host.endswith('.'+suffix) for suffix in
                ('gov.cn','edu.cn','ac.cn','cas.cn','nasa.gov','who.int','un.org'))
        primary=[row for row in usable if institutional(row)]
        if primary:
            usable=primary
        else:
            def site(row):
                labels=(urlsplit(row['url']).hostname or '').split('.')
                return '.'.join(labels[-3:] if '.'.join(labels[-2:]) in ('com.cn','net.cn','org.cn') else labels[-2:])
            if len({site(row) for row in usable})<2:
                usable=[]
        return usable[:request["result_limit"]]

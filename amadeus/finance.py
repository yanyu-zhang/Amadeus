"""Build a sourced, Sunday-based financial calendar from live public schedules."""

import asyncio
import json
import logging
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
import primp
from icalendar import Calendar
from lxml import html

from .factcheck import safe_text
from .meetings import save_json

LOG = logging.getLogger(__name__)
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BEA_URL = "https://www.bea.gov/news/schedule/full"
BLS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
MEMBERSHIP_URLS = {
    "S&P 500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "Nasdaq 100": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
}
MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        1,
    )
}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})
IMPORTANT_BLS = {
    "Consumer Price Index": "CPI 消费者价格指数",
    "Producer Price Index": "PPI 生产者价格指数",
    "Employment Situation": "非农就业／失业率",
    "Job Openings and Labor Turnover": "JOLTS 职位空缺",
    "Employment Cost Index": "就业成本指数",
}


class FinanceError(RuntimeError):
    pass


def fetch_bls_calendar():
    # The BLS site rejects the default aiohttp TLS/client fingerprint. Read the
    # same public calendar with the browser-compatible client used by DDGS.
    try:
        response = primp.Client(impersonate="chrome", timeout=20).get(BLS_URL)
    except Exception as error:
        raise ValueError("BLS 官方日历请求失败。") from error
    if response.status_code != 200 or len(response.content) > 3 * 1024 * 1024:
        raise ValueError("BLS 官方日历不可读取。")
    return response.content


def calendar_window(now):
    """The current Sunday-to-Saturday week, followed by the next complete week."""
    today = now.date()
    start = today - timedelta(days=(today.weekday() + 1) % 7)
    return start, start + timedelta(days=13)


def normalized_symbol(symbol):
    return symbol.strip().upper().replace(".", "-")


def parse_members(contents, index):
    document = html.fromstring(contents)
    members = {}
    for table in document.xpath("//table"):
        rows = table.xpath(".//tr")
        if not rows:
            continue
        headers = [cell.text_content().strip().casefold() for cell in rows[0].xpath("./th|./td")]
        symbol_column = next(
            (i for i, title in enumerate(headers) if title in ("symbol", "ticker")), None
        )
        if symbol_column is None:
            continue
        for row in rows[1:]:
            cells = row.xpath("./td")
            if len(cells) <= symbol_column + 1:
                continue
            symbol = normalized_symbol(cells[symbol_column].text_content())
            if re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)?", symbol):
                members[symbol] = cells[symbol_column + 1].text_content().strip()
        break
    lower, upper = (490, 520) if index == "S&P 500" else (95, 110)
    if not lower <= len(members) <= upper:
        raise ValueError(f"{index} 成分表结构或数量异常。")
    return members


def dated_event(day, title, url, category="macro", **extra):
    return {"date": day.isoformat(), "title": title, "url": url, "category": category, **extra}


def english_date(value):
    match = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", value)
    if not match or match[1] not in MONTHS:
        raise ValueError("无法读取来源日期。")
    return date(int(match[3]), MONTHS[match[1]], int(match[2]))


def parse_fed(contents, start, end):
    document = html.fromstring(contents)
    events, years = [], set()
    for heading in document.xpath('//div[contains(@class,"panel-heading")]'):
        match = re.search(r"(\d{4}) FOMC Meetings", heading.text_content())
        if not match:
            continue
        year = int(match[1])
        years.add(year)
        for row in heading.getparent().xpath(
            './/div[contains(concat(" ",normalize-space(@class)," ")," fomc-meeting ")]'
        ):
            months = row.xpath('.//div[contains(@class,"fomc-meeting__month")]/strong/text()')
            days = row.xpath('.//div[contains(@class,"fomc-meeting__date")]/text()')
            if not months or not days:
                raise ValueError("美联储会议日历结构异常。")
            month_names = months[0].strip().split("/")
            numbers = re.findall(r"\d+", days[0])
            if not numbers or "notation vote" in days[0]:
                continue
            first = date(year, MONTHS[month_names[0]], int(numbers[0]))
            last = date(year, MONTHS[month_names[-1]], int(numbers[-1]))
            if start <= first <= end and first != last:
                events.append(dated_event(first, "美联储 FOMC 议息会议（首日）", FED_URL))
            if start <= last <= end:
                title = "美联储 FOMC 议息会议（决议日）"
                if "*" in days[0]:
                    title += "；附经济预测"
                events.append(dated_event(last, title, FED_URL))
            minutes = row.xpath('.//div[contains(@class,"fomc-meeting__minutes")]')
            if minutes and "Released" in minutes[0].text_content():
                released = english_date(minutes[0].text_content().split("Released", 1)[1])
                if start <= released <= end:
                    events.append(dated_event(released, "美联储 FOMC 会议纪要发布", FED_URL))
    missing = {start.year, end.year} - years
    if missing:
        raise ValueError("美联储尚未提供查询年份的完整会议日程。")
    return events


def local_release(day, clock, timezone):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(AM|PM)", clock.strip(), re.IGNORECASE)
    if not match:
        raise ValueError("发布时刻格式异常。")
    hour = int(match[1]) % 12 + (12 if match[3].upper() == "PM" else 0)
    return datetime(
        day.year, day.month, day.day, hour, int(match[2]), tzinfo=ZoneInfo("America/New_York")
    ).astimezone(timezone)


def parse_bea(contents, start, end, timezone):
    document = html.fromstring(contents)
    tables = document.xpath('//table[@id="release-schedule-table"]')
    if not tables:
        raise ValueError("BEA 日程结构异常。")
    events, years = [], set()
    for table in tables:
        heading = table.xpath(".//thead")[0].text_content()
        match = re.search(r"\b(20\d{2})\b", heading)
        if not match:
            raise ValueError("BEA 日程缺少年份。")
        year = int(match[1])
        years.add(year)
        for row in table.xpath(".//tbody/tr"):
            title_nodes = row.xpath('.//td[contains(@class,"release-title")]')
            if not title_nodes:
                continue
            original = title_nodes[0].text_content().strip()
            if original.startswith("GDP") or "Gross Domestic Product" in original:
                title = "美国 GDP 发布"
            elif "Personal Income and Outlays" in original:
                title = "美国个人收入与支出／PCE 通胀"
            else:
                continue
            dates = row.xpath('.//div[contains(@class,"release-date")]/text()')
            if not dates:
                raise ValueError("BEA 日程缺少日期。")
            day = english_date(f"{dates[0]}, {year}")
            clocks = row.xpath(".//small/text()")
            when = local_release(day, clocks[0], timezone) if clocks else None
            local_day = when.date() if when else day
            if start <= local_day <= end:
                events.append(
                    dated_event(
                        local_day,
                        title,
                        BEA_URL,
                        time=when.strftime("%H:%M") if when else "",
                        detail=original,
                    )
                )
    if {start.year, end.year} - years:
        raise ValueError("BEA 日程尚未覆盖查询年份。")
    return events


def parse_bls(contents, start, end, timezone):
    calendar = Calendar.from_ical(contents)
    entries = calendar.walk("VEVENT")
    if not entries:
        raise ValueError("BLS 日历没有可读取的日程。")
    events, scheduled_dates = [], []
    for entry in entries:
        original = str(entry.get("SUMMARY", ""))
        title = next((title for phrase, title in IMPORTANT_BLS.items() if phrase in original), None)
        if "DTSTART" not in entry:
            continue
        when = entry.decoded("DTSTART")
        if isinstance(when, datetime):
            if when.tzinfo is None:
                when = when.replace(tzinfo=ZoneInfo("America/New_York"))
            when = when.astimezone(timezone)
            day, clock = when.date(), when.strftime("%H:%M")
        else:
            day, clock = when, ""
        scheduled_dates.append(day)
        if title and start <= day <= end:
            events.append(dated_event(day, title, BLS_URL, time=clock, detail=original))
    if not scheduled_dates or max(scheduled_dates) < end:
        raise ValueError("BLS 日历尚未完整覆盖查询区间。")
    return events


def parse_earnings(payload, day, members):
    if payload.get("status", {}).get("rCode") != 200:
        raise ValueError("Nasdaq 财报日历返回错误。")
    data = payload.get("data")
    if not isinstance(data, dict) or "rows" not in data:
        raise ValueError("Nasdaq 财报日历缺少日程数据。")
    # 'asOf' is checked when present so a response for another date is not reused.
    if data.get("asOf") and english_date(data["asOf"]) != day:
        raise ValueError("Nasdaq 财报日历的日期与请求不一致。")
    rows = data["rows"]
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise TypeError("Nasdaq 财报列表格式异常。")
    events = []
    for row in rows:
        symbol = normalized_symbol(row.get("symbol", ""))
        indexes = [index for index, stocks in members.items() if symbol in stocks]
        if not indexes:
            continue
        period = {"time-pre-market": "盘前", "time-after-hours": "盘后"}.get(
            row.get("time"), "时间未提供"
        )
        name = row.get("name") or members[indexes[0]][symbol]
        cap = re.sub(r"[^0-9.]", "", row.get("marketCap") or "")
        events.append(
            dated_event(
                day,
                name,
                f"https://www.nasdaq.com/market-activity/earnings?date={day.isoformat()}",
                category="earnings",
                symbol=symbol,
                indexes=indexes,
                period=period,
                estimated=True,
                market_cap=float(cap) if cap else 0,
            )
        )
    return events


def render_calendar(record):
    lines = [
        "# Amadeus 两周财经日历",
        f"区间：{record['start']}（周日）至 {record['end']}（周六）",
        "范围：本周＋下周，包含本周已过去的日期。",
        f"宏观时刻：{record['timezone']}；财报日期按美股交易日，盘前／盘后按美股常规交易时段。",
        f"更新：{record['checked_at']}",
        "财报均为 Nasdaq 日历预计日期，可能调整；不代表公司已正式确认。",
    ]
    if record["warnings"]:
        lines.append(
            "**数据覆盖提醒**\n" + "\n".join(f"- {warning}" for warning in record["warnings"])
        )
    for offset, week in ((0, "本周"), (7, "下周")):
        first = date.fromisoformat(record["start"]) + timedelta(days=offset)
        last = first + timedelta(days=6)
        lines.append(f"## {week}：{first} — {last}")
        events = [
            event
            for event in record["events"]
            if first.isoformat() <= event["date"] <= last.isoformat()
        ]
        if not events:
            lines.append("已成功读取的来源中未列出符合范围的事件；请同时查看数据覆盖提醒。")
        previous = None
        for event in events:
            if event["date"] != previous:
                day = date.fromisoformat(event["date"])
                lines.append(f"### {day} 周{'一二三四五六日'[day.weekday()]}")
                previous = event["date"]
            if event["category"] == "earnings":
                label = f"{event['symbol']} · {safe_text(event['title'])} — 财报（预计）· {event['period']} · {' / '.join(event['indexes'])}"
            else:
                label = f"{event.get('time') or '时间未提供'} · {safe_text(event['title'])}"
                if event.get("detail"):
                    label += " · " + safe_text(event["detail"])
            lines.append(f"- {label} [来源](<{event['url']}>)")
    lines.append(
        "## 成分股筛选依据\n"
        + "\n".join(
            f"- [{index} 成分表](<{info['url']}>)：{info['count']} 个证券代码，读取于 {info['checked_at']}。"
            for index, info in record["membership"].items()
        )
    )
    lines.append(
        "公司范围取两个指数的并集，同一公司代码不会因同时入选两个指数而重复列出。成分表为公开第三方资料，存在更新滞后的可能。"
    )
    return "\n\n".join(lines)


def render_preview(record):
    macro = [event for event in record["events"] if event["category"] == "macro"]
    earnings = [event for event in record["events"] if event["category"] == "earnings"]
    lines = [
        f"**两周财经日历：{record['start']} — {record['end']}**",
        f"周日开始，本周＋下周（含已过去的日期）。时区：{record['timezone']}。",
        f"读取到 {len(macro)} 项宏观事件、{len(earnings)} 项指数成分股预计财报。",
    ]
    if record["warnings"]:
        lines.append("**部分来源缺失：**" + "；".join(record["warnings"]))
    highlights = ["**宏观事件**"]
    highlights.extend(
        f"- {event['date'][5:]} {event.get('time', '')} · {safe_text(event['title'])} [来源](<{event['url']}>)"
        for event in macro
    )
    if not macro:
        highlights.append("已读取的来源未列出宏观事件；缺失来源可能包含其他事件。")
    highlights.append("**重点预计财报（市值最高的 10 家公司）**")
    largest = sorted(earnings, key=lambda event: (-event["market_cap"], event["symbol"]))[:10]
    highlights.extend(
        f"- {event['date'][5:]} · {event['symbol']} · {safe_text(event['title'])} · {event['period']} · {' / '.join(event['indexes'])} [来源](<{event['url']}>)"
        for event in sorted(largest, key=lambda event: (event["date"], event["symbol"]))
    )
    if not earnings:
        highlights.append("已读取的来源未列出成分股财报；缺失日期不代表没有财报。")
    footer = (
        "财报日期均为预计，可能调整；点击下方按钮查看完整日历与覆盖缺口。\n更新："
        + record["checked_at"]
    )
    return "\n".join([*lines, *highlights, footer])


def calendar_pages(body):
    pages, current = [], ""
    for line in body.splitlines():
        if current and len(current) + len(line) + 1 > 1800:
            pages.append(current.rstrip())
            current = ""
        while len(line) > 1800:
            pages.append(line[:1800])
            line = line[1800:]
        current += line + "\n"
    if current.strip():
        pages.append(current.rstrip())
    return pages or ["没有可显示的日历内容。"]


class CalendarPages(discord.ui.View):
    def __init__(self, body):
        super().__init__(timeout=1800)
        self.pages = calendar_pages(body)
        self.page = 0
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page == len(self.pages) - 1

    def content(self):
        return (
            f"**完整财经日历 · 第 {self.page + 1}/{len(self.pages)} 页**\n\n{self.pages[self.page]}"
        )

    async def move(self, interaction, delta):
        self.page = max(0, min(len(self.pages) - 1, self.page + delta))
        self.update_buttons()
        await interaction.response.edit_message(
            content=self.content(),
            view=self,
            suppress_embeds=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="上一页", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.move(interaction, -1)

    @discord.ui.button(label="下一页", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.move(interaction, 1)


class CalendarDetails(discord.ui.View):
    def __init__(self, body):
        super().__init__(timeout=1800)
        self.body = body

    @discord.ui.button(label="查看完整日历", style=discord.ButtonStyle.secondary)
    async def details(self, interaction: discord.Interaction, button: discord.ui.Button):
        pages = CalendarPages(self.body)
        await interaction.response.send_message(
            pages.content(),
            view=pages,
            ephemeral=True,
            suppress_embeds=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class FinanceCalendar:
    def __init__(self, timezone, root="data/finance"):
        self.timezone = ZoneInfo(timezone)
        self.root = Path(root)
        self.lock = asyncio.Lock()
        self.cached = None

    async def report(self, refresh=False, progress=None, now=None):
        if self.lock.locked():
            raise FinanceError("正在查询财经日历，请完成后再试。")
        async with self.lock:
            now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
            start, end = calendar_window(now)
            if not refresh and self.cached:
                saved, path, body = self.cached
                ttl = 300 if saved["warnings"] else 1800
                if (
                    saved["start"] == start.isoformat()
                    and 0
                    <= (now - datetime.fromisoformat(saved["checked_at"])).total_seconds()
                    < ttl
                ):
                    return path, body, saved
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            record = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timezone": self.timezone.key,
                "checked_at": now.isoformat(timespec="seconds"),
                "events": [],
                "warnings": [],
                "membership": {},
                "coverage": [],
            }
            started = time.monotonic()

            async def update(message):
                LOG.info("finance=%s %s", start, message)
                if progress:
                    await progress(message)

            try:
                connector = aiohttp.TCPConnector(limit=3)
                async with aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; Amadeus-FinancialCalendar/1.0)",
                        "Accept": "application/json,text/html,text/calendar,*/*",
                        "Origin": "https://www.nasdaq.com",
                    },
                ) as web:

                    async def fetch(url):
                        if url == BLS_URL:
                            return await asyncio.to_thread(fetch_bls_calendar)
                        async with web.get(url) as response:
                            response.raise_for_status()
                            body = bytearray()
                            async for chunk in response.content.iter_chunked(65536):
                                body.extend(chunk)
                                if len(body) > 3 * 1024 * 1024:
                                    raise ValueError("日历来源超出读取上限。")
                            return bytes(body)

                    await update("正在读取标普 500 和纳斯达克 100 成分表。")
                    members = {}
                    for index, url in MEMBERSHIP_URLS.items():
                        cache_path = self.root / (
                            "sp500.members.json" if index == "S&P 500" else "nasdaq100.members.json"
                        )
                        cache = None
                        if cache_path.exists():
                            try:
                                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                                age = (
                                    now - datetime.fromisoformat(cache["checked_at"])
                                ).total_seconds()
                                if (
                                    not isinstance(cache["members"], dict)
                                    or not 0 <= age < 7 * 86400
                                ):
                                    cache = None
                            except (ValueError, KeyError, OSError):
                                cache = None
                        if cache and not refresh and age < 86400:
                            info = cache
                        else:
                            try:
                                stocks = await asyncio.to_thread(
                                    parse_members, await fetch(url), index
                                )
                                info = {
                                    "members": stocks,
                                    "checked_at": now.isoformat(timespec="seconds"),
                                    "url": url,
                                }
                                save_json(cache_path, info)
                            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                                LOG.warning(
                                    "finance=%s membership=%s failed=%s",
                                    start,
                                    index,
                                    type(error).__name__,
                                )
                                if not cache:
                                    record["warnings"].append(
                                        f"{index} 成分表未能读取，该指数独有公司的财报可能遗漏。"
                                    )
                                    continue
                                info = cache
                                record["warnings"].append(
                                    f"{index} 成分表更新失败，暂用 {cache['checked_at']} 的缓存。"
                                )
                        members[index] = info["members"]
                        record["membership"][index] = {
                            "url": info["url"],
                            "checked_at": info["checked_at"],
                            "count": len(info["members"]),
                        }
                    if not members:
                        raise FinanceError(
                            "两个指数成分表均不可用，无法可靠筛选财报公司，请稍后重试。"
                        )

                    await update("正在读取美联储与美国重要经济数据发布日程。")

                    async def macro(name, url, parser):
                        try:
                            events = await asyncio.to_thread(parser, await fetch(url), start, end)
                            record["events"].extend(events)
                            record["coverage"].append(
                                {"source": name, "url": url, "status": "ok", "events": len(events)}
                            )
                        except (
                            aiohttp.ClientError,
                            TimeoutError,
                            ValueError,
                            KeyError,
                            IndexError,
                        ) as error:
                            record["warnings"].append(f"{name} 日程读取失败，相关事件可能遗漏。")
                            record["coverage"].append(
                                {
                                    "source": name,
                                    "url": url,
                                    "status": "failed",
                                    "error": type(error).__name__,
                                }
                            )
                            LOG.warning(
                                "finance=%s source=%s failed=%s", start, name, type(error).__name__
                            )

                    await asyncio.gather(
                        macro("美联储 FOMC", FED_URL, parse_fed),
                        macro(
                            "BEA（GDP／PCE）",
                            BEA_URL,
                            lambda body, first, last: parse_bea(body, first, last, self.timezone),
                        ),
                        macro(
                            "BLS（CPI／PPI／非农就业）",
                            BLS_URL,
                            lambda body, first, last: parse_bls(body, first, last, self.timezone),
                        ),
                    )
                    await update("正在查询两周财报日历，并按两个指数筛选公司。")

                    async def earnings(day):
                        url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}"
                        try:
                            events = parse_earnings(json.loads(await fetch(url)), day, members)
                            record["events"].extend(events)
                            record["coverage"].append(
                                {
                                    "source": "Nasdaq 财报",
                                    "date": day.isoformat(),
                                    "url": url,
                                    "status": "ok",
                                    "events": len(events),
                                }
                            )
                        except (
                            aiohttp.ClientError,
                            TimeoutError,
                            ValueError,
                            KeyError,
                            TypeError,
                        ) as error:
                            record["coverage"].append(
                                {
                                    "source": "Nasdaq 财报",
                                    "date": day.isoformat(),
                                    "url": url,
                                    "status": "failed",
                                    "error": type(error).__name__,
                                }
                            )
                            LOG.warning(
                                "finance=%s earnings_date=%s failed=%s",
                                start,
                                day,
                                type(error).__name__,
                            )

                    await asyncio.gather(*(earnings(start + timedelta(days=i)) for i in range(14)))
                    missing = sorted(
                        row["date"]
                        for row in record["coverage"]
                        if row["source"] == "Nasdaq 财报" and row["status"] == "failed"
                    )
                    if missing:
                        record["warnings"].append(
                            "以下日期的财报日历未能读取："
                            + "、".join(missing)
                            + "；不能视为没有财报。"
                        )
                unique = {
                    (
                        event["date"],
                        event["category"],
                        event.get("symbol", ""),
                        event["title"],
                    ): event
                    for event in record["events"]
                }
                record["events"] = sorted(
                    unique.values(),
                    key=lambda event: (
                        event["date"],
                        0 if event["category"] == "macro" else 1,
                        event.get("time", ""),
                        event.get("symbol", ""),
                        event["title"],
                    ),
                )
                record["elapsed_seconds"] = round(time.monotonic() - started, 2)
                record["status"] = "partial" if record["warnings"] else "complete"
                directory = (
                    self.root / start.isoformat() / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                )
                directory.mkdir(parents=True, mode=0o700)
                body = render_calendar(record)
                path = directory / "report.md"
                path.write_text(body, encoding="utf-8")
                save_json(directory / "events.json", record)
                self.cached = record, path, body
                LOG.info(
                    "finance=%s 查询完成 events=%d warnings=%d elapsed=%.2fs path=%s",
                    start,
                    len(record["events"]),
                    len(record["warnings"]),
                    record["elapsed_seconds"],
                    path.resolve(),
                )
                return path, body, record
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                raise FinanceError("财经日历查询未完成，请检查网络后重试。") from error


def register_finance_command(bot):
    from discord import app_commands

    @bot.tree.command(name="finance", description="查看本周与下周的重要财经事件（周日开始）")
    @app_commands.guild_only()
    @app_commands.describe(refresh="跳过缓存，重新联网查询（默认否）")
    @app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
    async def finance(interaction: discord.Interaction, refresh: bool = False):
        if not (
            interaction.app_permissions.send_messages and interaction.app_permissions.embed_links
        ):
            await interaction.response.send_message(
                "我需要发送消息和嵌入链接权限。", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)

        async def progress(message):
            await interaction.edit_original_response(content="Amadeus：" + message)

        try:
            _, body, record = await asyncio.wait_for(
                bot.finance.report(refresh, progress), timeout=240
            )
        except (FinanceError, TimeoutError) as error:
            await interaction.edit_original_response(
                content=str(error) or "财经日历查询超时，请稍后重试。"
            )
            return
        preview = calendar_pages(render_preview(record))
        details = CalendarDetails(body)
        await interaction.edit_original_response(
            content=preview[0],
            attachments=[],
            view=details if len(preview) == 1 else None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        for index, page in enumerate(preview[1:], start=1):
            kwargs = {"view": details} if index == len(preview) - 1 else {}
            await interaction.followup.send(
                page,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
                **kwargs,
            )

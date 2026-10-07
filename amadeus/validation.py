import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def poll_options(question: str, options: str) -> tuple[str, list[str]]:
    question = question.strip()
    answers = [answer.strip() for answer in options.split("|")]
    if not 1 <= len(question) <= 300:
        raise ValueError("问题需要 1–300 个字符。")
    if not 2 <= len(answers) <= 10 or any(not answer for answer in answers):
        raise ValueError("请用 | 分隔 2–10 个非空选项，例如：披萨 | 寿司 | 火锅。")
    if any(len(answer) > 55 for answer in answers):
        raise ValueError("每个选项最多 55 个字符。")
    if len({answer.casefold() for answer in answers}) != len(answers):
        raise ValueError("投票选项不能重复。")
    return question, answers


def meeting_form(
    title: str,
    start_date: str,
    end_date: str,
    start_hour: int,
    end_hour: int,
    timezone: str,
) -> dict[str, str]:
    title = title.strip()
    if not 1 <= len(title) <= 100:
        raise ValueError("活动名称需要 1–100 个字符。")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("请使用有效时区，例如 America/Los_Angeles 或 Asia/Shanghai。") from None
    year = datetime.now(zone).year

    def parse_date(value: str) -> date:
        value = value.strip()
        match = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", value)
        if match:
            return date(year, int(match[1]), int(match[2]))
        return date.fromisoformat(value)

    try:
        start, end = parse_date(start_date), parse_date(end_date)
    except ValueError:
        raise ValueError(
            f"请输入有效月日，例如 10-12 或 10/12（自动使用 {year} 年）；也支持 YYYY-MM-DD。"
        ) from None
    days = (end - start).days + 1
    if not 1 <= days <= 31:
        raise ValueError("结束日期不能早于开始日期，日期范围最多 31 天。")
    if not 0 <= start_hour < end_hour <= 24:
        raise ValueError("时间需满足 0 ≤ 开始小时 < 结束小时 ≤ 24；跨夜请拆开安排。")
    return {
        "NewEventName": title,
        "DateTypes": "SpecificDates",
        "PossibleDates": "|".join((start + timedelta(days=i)).isoformat() for i in range(days)),
        "NoEarlierThan": str(start_hour),
        "NoLaterThan": str(end_hour % 24),
        "TimeZone": timezone,
    }

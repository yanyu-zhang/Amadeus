from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from .logging_setup import configure_logging
from .meeting_commands import register_meeting_commands
from .meetings import MeetingManager
from .validation import meeting_form, poll_options
from .when2meet import When2meetError, create_event

LOG = logging.getLogger(__name__)
COLOR = 0xA43845


class Amadeus(discord.Client):
    def __init__(self, guild_id: Optional[int], timezone: str):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.guild_id = guild_id
        self.timezone = timezone
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.meetings = MeetingManager(self)
        register_commands(self)
        register_meeting_commands(self)
        self.tree.error(self.command_error)

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        await self.change_presence(activity=discord.Game(name="Amadeus 在线 · /help"))
        LOG.info("Amadeus connected as %s", self.user)

    async def close(self):
        await self.meetings.close()
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"稍等一下，{error.retry_after:.0f} 秒后再试。"
        else:
            LOG.error("Slash command failed", exc_info=(type(error), error, error.__traceback__))
            message = "操作失败。请检查我的频道权限，或稍后再试。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def register_commands(bot: Amadeus):
    @bot.tree.command(name="poll", description="让 Amadeus 创建一个投票")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    @app_commands.describe(
        question="投票问题",
        options="用 | 分隔选项，例如：披萨 | 寿司 | 火锅",
        hours="持续小时数（1–768，默认 24）",
        multiple="是否允许多选",
    )
    async def poll(
        interaction: discord.Interaction,
        question: str,
        options: str,
        hours: app_commands.Range[int, 1, 768] = 24,
        multiple: bool = False,
    ):
        try:
            question, answers = poll_options(question, options)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        if not (
            interaction.app_permissions.send_messages and interaction.app_permissions.send_polls
        ):
            await interaction.response.send_message(
                "我需要此频道的发送消息和发送投票权限。", ephemeral=True
            )
            return
        ballot = discord.Poll(question=question, duration=timedelta(hours=hours), multiple=multiple)
        for answer in answers:
            ballot.add_answer(text=answer)
        await interaction.response.send_message(
            "Amadeus：实验室投票已开启。让我看看大家的判断。", poll=ballot
        )

    @bot.tree.command(name="when2meet", description="创建 When2meet 链接，让大家填写可聚会时间")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
    @app_commands.describe(
        title="活动名称",
        start_date="开始月日，如 10-12 或 10/12（自动使用当前年份）",
        end_date="结束月日，如 10-15 或 10/15（包含当天，自动使用当前年份）",
        start_hour="每天开始小时（0–23，默认 9）",
        end_hour="每天结束小时（1–24，默认 22）",
        timezone="IANA 时区，例如 America/Los_Angeles 或 Asia/Shanghai",
    )
    async def when2meet(
        interaction: discord.Interaction,
        title: str,
        start_date: str,
        end_date: str,
        start_hour: app_commands.Range[int, 0, 23] = 9,
        end_hour: app_commands.Range[int, 1, 24] = 22,
        timezone: Optional[str] = None,
    ):
        timezone = timezone or bot.timezone
        try:
            form = meeting_form(title, start_date, end_date, start_hour, end_hour, timezone)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        if not (
            interaction.app_permissions.send_messages and interaction.app_permissions.embed_links
        ):
            await interaction.response.send_message(
                "我需要此频道的发送消息和嵌入链接权限。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            assert bot.http_session is not None
            url = await create_event(bot.http_session, form)
        except When2meetError as error:
            await interaction.followup.send(
                f"{error} 可以先到 https://www.when2meet.com/ 手动创建。"
                "如果请求超时，活动可能已创建，请避免连续重试。",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title=form["NewEventName"],
            url=url,
            color=COLOR,
            description="Amadeus：时间协调交给我。点击下面的按钮，填写你有空的时间。",
        )
        dates = form["PossibleDates"].split("|")
        embed.add_field(name="日期", value=f"{dates[0]} → {dates[-1]}", inline=False)
        embed.add_field(name="每天的时间", value=f"{start_hour:02d}:00–{end_hour:02d}:00")
        embed.add_field(name="时区", value=timezone)
        embed.set_footer(text="Amadeus · Future Gadget Laboratory")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="填写可用时间", url=url))
        # Resolve the private defer before sending a separate public followup.
        await interaction.edit_original_response(content=f"活动已创建：{url}")
        try:
            # Publish only after creation succeeds; keep errors private.
            await interaction.followup.send(embed=embed, view=view, ephemeral=False, wait=True)
        except discord.HTTPException:
            await interaction.followup.send(
                f"活动已创建，但频道发布失败。链接：{url}", ephemeral=True
            )
            return
        await interaction.edit_original_response(content=f"已发布排期活动：{url}")

    @bot.tree.command(name="help", description="查看 Amadeus 的功能")
    async def help_command(interaction: discord.Interaction):
        embed = discord.Embed(
            title="Amadeus 已连接",
            color=COLOR,
            description=(
                "我是 Amadeus。先从实验室的投票和时间协调开始吧。\n\n"
                "**/poll** — 问题、以 `|` 分隔的选项；支持多选和投票时长。\n"
                "**/when2meet** — 活动名、起止月日（如 `10/12`）、每天的时间范围和时区。"
                "年份按活动时区自动补全；也支持完整日期。\n\n"
                "**/meeting start** — 加入你的语音频道，记录中文会议。\n"
                "**/meeting stop** — 停止记录并生成中文总结。\n"
                "**/meeting status** — 查看录音和转写状态。\n"
                "**/meeting summary** — 重试总结或恢复已保存的记录。\n\n"
                "投票和排期会公开发布在当前频道。投票选择不是匿名的；"
                "排期参与者在 When2meet 页面填写可用时间。"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


def main():
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("请在 .env 中设置 DISCORD_TOKEN，参考 .env.example。")
    raw_guild = os.getenv("DISCORD_GUILD_ID", "").strip()
    if raw_guild and (not raw_guild.isdecimal() or int(raw_guild) <= 0):
        raise SystemExit("DISCORD_GUILD_ID 必须是有效的 Discord 服务器数字 ID。")
    timezone = os.getenv("DEFAULT_TIMEZONE", "America/Los_Angeles").strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit("DEFAULT_TIMEZONE 必须是有效的 IANA 时区。") from None
    configure_logging()

    async def run():
        async with Amadeus(int(raw_guild) if raw_guild else None, timezone) as bot:
            await bot.start(token)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOG.info("Amadeus 已停止。")
    except discord.LoginFailure:
        raise SystemExit("Discord token 无效，请在 Developer Portal 重置并更新 .env。") from None

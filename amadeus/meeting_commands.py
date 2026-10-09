import asyncio

import discord
from discord import app_commands


def can_manage(interaction, meeting):
    return (
        interaction.user.id == meeting.metadata["owner_id"] or interaction.permissions.manage_guild
    )


def register_meeting_commands(bot):
    group = app_commands.Group(
        name="meeting", description="中文会议记录和本地总结", guild_only=True
    )

    @group.command(name="start", description="加入你所在的语音频道，开始记录中文会议")
    @app_commands.describe(title="会议名称（默认：实验室会议）")
    @app_commands.checks.cooldown(1, 15, key=lambda i: (i.guild_id, i.user.id))
    async def start(interaction: discord.Interaction, title: str = "实验室会议"):
        title = title.strip()
        if not 1 <= len(title) <= 100:
            await interaction.response.send_message("会议名称需要 1–100 个字符。", ephemeral=True)
            return
        voice_state = interaction.user.voice
        channel = voice_state.channel if voice_state else None
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "请先加入一个普通语音频道，再开始会议。", ephemeral=True
            )
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.connect):
            await interaction.response.send_message(
                "我需要该语音频道的查看频道和连接权限。", ephemeral=True
            )
            return
        if not (
            interaction.app_permissions.send_messages and interaction.app_permissions.attach_files
        ):
            await interaction.response.send_message(
                "我需要当前文字频道的发送消息和附加文件权限。", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        await interaction.edit_original_response(
            content=(
                f"准备记录会议：{title}。我会加入 {channel.mention}，持续接收大家的语音。"
                "音频和转写在本机保存、处理；结束后中文总结会发布到本频道。"
                "请让参会者知晓。使用 /meeting stop 结束。"
            )
        )
        try:
            meeting = await bot.meetings.start(
                interaction.guild,
                channel,
                interaction.channel,
                interaction.user,
                title,
            )
        except (TimeoutError, RuntimeError, discord.ClientException) as error:
            await interaction.edit_original_response(content=f"未开始记录：{error}")
            return
        await interaction.edit_original_response(
            content=(
                f"Amadeus 已加入 {channel.mention}，开始记录 **{title}**。会议 ID：`{meeting.id}`。\n"
                "语音在本机按发言者分段保存，并用中文模型转写；"
                "结束后总结会发布到本频道。使用 /meeting status 查看状态，/meeting stop 结束。"
            )
        )

    @group.command(name="stop", description="停止记录并离开语音频道，自动生成中文会议总结")
    async def stop(interaction: discord.Interaction):
        meeting = bot.meetings.active.get(interaction.guild_id)
        if meeting is None:
            await interaction.response.send_message("当前没有正在记录的会议。", ephemeral=True)
            return
        if not can_manage(interaction, meeting):
            await interaction.response.send_message(
                "仅会议发起者或有管理服务器权限的人可以结束会议。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            meeting = await bot.meetings.stop(interaction.guild_id)
        except RuntimeError as error:
            await interaction.edit_original_response(content=str(error))
            return
        await interaction.edit_original_response(
            content=(
                f"会议 `{meeting.id}` 已停止记录，我已离开语音频道。"
                "正在完成本地转写和中文总结，完成后会发到开始会议的文字频道。"
            )
        )

    @group.command(name="status", description="查看当前会议的录音和转写进度")
    async def status(interaction: discord.Interaction):
        try:
            meeting = bot.meetings.active.get(interaction.guild_id) or bot.meetings.find(
                interaction.guild_id
            )
        except RuntimeError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        labels = {
            "recording": "正在记录",
            "processing": "正在转写和总结",
            "complete": "总结已完成",
            "partial": "总结已完成（部分转写失败）",
            "summary_failed": "总结失败，可重试",
            "publish_failed": "总结已保存，频道发送失败",
            "connecting": "正在连接",
            "connection_failed": "连接失败",
        }
        state = labels.get(meeting.metadata["status"], meeting.metadata["status"])
        if meeting.stopped and meeting.metadata["status"] in ("recording", "connecting"):
            state = "重启前的记录已保存，可用 /meeting summary 恢复"
        text = f"会议：{meeting.metadata['title']}\nID：`{meeting.id}`\n状态：{state}"
        if meeting.sink:
            text += (
                f"\n已接收语音包：{meeting.sink.packets}"
                f"\n已转写音频段：{meeting.transcribed}/{meeting.captured}"
                f"\n录音大小：{meeting.sink.total_bytes / 1024 / 1024:.1f} MB"
            )
            if not meeting.sink.packets and not meeting.stopped:
                text += "\n尚未收到可解码语音；请发言后再检查，确认 bot 没有被服务器设为耳聋。"
        await interaction.response.send_message(text, ephemeral=True)

    @group.command(name="summary", description="重试本地总结，或恢复重启前的已保存会议记录")
    @app_commands.describe(meeting_id="开始时间戳会议 ID（也支持旧 ID）；省略则使用最近一场会议")
    async def summary(interaction: discord.Interaction, meeting_id: str = ""):
        try:
            meeting = bot.meetings.find(interaction.guild_id, meeting_id or None)
            if not can_manage(interaction, meeting):
                raise RuntimeError("仅会议发起者或有管理服务器权限的人可以生成总结。")
            if (
                interaction.guild_id in bot.meetings.active
                and bot.meetings.active[interaction.guild_id] is meeting
            ):
                raise RuntimeError("请先 /meeting stop，再生成总结。")
            if meeting.finishing and not meeting.finishing.done():
                raise RuntimeError("正在生成总结，请用 /meeting status 查看进度。")
            bot.meetings.sessions[meeting.id] = meeting
            meeting.metadata["status"] = "processing"
            meeting.persist()
            meeting.finishing = asyncio.create_task(bot.meetings._finish(meeting))
            meeting.finishing.add_done_callback(bot.meetings._task_done)
        except RuntimeError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(
            f"正在处理会议 `{meeting.id}`，完成后会发到开始会议的文字频道。",
            ephemeral=True,
        )

    bot.tree.add_command(group)

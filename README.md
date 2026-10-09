# Amadeus

以《命运石之门》的 Amadeus 为主题的 Python 3 Discord bot。支持投票、聚会时间协调、中文语音会议总结、联网事实核查和两周财经日历。

## 两周财经日历

在文字频道使用 `/finance`，按 `DEFAULT_TIMEZONE`（默认洛杉矶）查询本周和下周：当前周日到下周周六，包含本周已过去的日期。每次调用重新计算区间，跨月、跨年和夏令时按时区处理，不需要填写日期。`/finance refresh:true` 跳过缓存并重新联网查询。结果公开在调用频道，需要发送消息和嵌入链接权限。

直接读取[美联储 FOMC 官方会议日历](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)，列出议息会议首日、决议日、经济预测标记和已公布日期的会议纪要；还读取 [BLS 发布日历](https://www.bls.gov/schedule/news_release/bls.ics)的 CPI、PPI、非农就业、JOLTS、就业成本指数，以及 [BEA 发布日程](https://www.bea.gov/news/schedule/full)中的 GDP 和个人收入与支出／PCE。来源提供具体时间时转换为配置时区；未提供时不猜测。

逐日查询 [Nasdaq 财报日历](https://www.nasdaq.com/market-activity/earnings)，仅保留标普 500 或纳斯达克 100 成分股，两个指数取并集，显示英文公司名、证券代码、所属指数和盘前／盘后。成分股来自实时读取的公开 Wikipedia 成分表，标注来源和读取时间；第三方成分表可能更新滞后。Nasdaq 日期一律标为预计，不当作公司正式确认；不预测财报结果。频道显示宏观事件与两周内有财报、日历市值最高的 10 家公司；不足 10 家时全部显示。选取后按财报日期展示，汇总过长时分成多条文字消息，确保不因长度限制丢失公司，不自动发送 Markdown 附件。点击“查看完整日历”可分页查看全部财报和来源，详细内容仅点击者可见，按钮有效期为 30 分钟；失效后重新运行 `/finance`。本地 `report.md` 仍保留。

不需要新 API key，也不调用语言模型编造日程。来源拒绝访问、格式异常、年份尚未覆盖或某日查询失败时，会逐项提示覆盖缺口，不把查询失败视为没有事件。完整查询缓存 30 分钟，不完整查询缓存 5 分钟；成分表缓存 24 小时，更新失败时最多使用 7 天内的旧表并明确注明。`data/finance/<本周周日>/<查询时间>/` 保存 `report.md` 和 `events.json`（事件、来源、覆盖状态和耗时），被 Git 忽略。日志以 `finance=<本周周日>` 显示进度。

## 联网事实核查

在文字频道使用 `/factcheck claim:阿波罗11号于1969年首次载人登月`。结果公开在当前频道，给出有依据、与来源矛盾、部分有依据或证据不足，并附实际读取的来源链接。需要发送消息、嵌入链接和附加文件权限。最多输入 800 字符，同一时间处理一个核查任务。

使用与会议总结相同的本机 `OLLAMA_MODEL`（默认 Qwen3.5 9B）。模型规划搜索，bot 通过 [DDGS](https://github.com/deedy5/ddgs) 的公开搜索入口检索并读取网页正文，再交给本地模型核查；无需新增 API key。最多两轮检索、六个网页，优先官方及原始资料。搜索查询会发送给搜索引擎，结果发布到 Discord；不会自动上传会议录音或转写进行核查。

引用只能指向真实读取的网页片段，URL 由程序生成；搜索摘要或读取失败不能单独支撑确定结论。结论生成后再次核对引用支持；不通过时修正一次，仍无支持则写证据不足。搜索无结果时改写搜索词重试一次。网页可能不可访问、过时或有误，模型也可能误判；证据不足时明确说明，搜不到不等于错误。当前读取 HTML 和纯文本，暂不解析 PDF 或依赖 JavaScript 的页面。核查与会议总结的模型请求串行执行，避免同时占用大量模型上下文；录音仍可继续。

日志显示 `factcheck=<核查ID>` 的规划、搜索、阅读和核对进度。`data/factchecks/<核查ID>/` 保存 `report.md`、`evidence.json`（查询、网页片段和引用）及 `metrics.json`（模型和耗时），不进入 Git。

## 功能

- `/poll question:今晚吃什么 options:披萨 | 寿司 | 火锅 hours:24 multiple:false`
  创建 Discord 原生投票，支持 2–10 个选项、多选和 1–768 小时的持续时间。投票由 Discord 保存，bot 重启不影响已有投票。投票不是匿名的。
- `/when2meet title:实验室聚会 start_date:10/10 end_date:10/12 start_hour:18 end_hour:23 timezone:America/Los_Angeles`
  创建真实的 When2meet 活动并在当前频道发布链接按钮。日期只需输入月日（`10/10`、`10-10` 或 `1/5`），每次调用时按活动时区读取当前年份并补全；仍支持 `YYYY-MM-DD`，跨年时可使用完整日期。起止日期均包含当天，最多 31 天。参与者打开网页填写姓名和可用时间。每天时间支持整点，不支持跨夜；`end_hour:24` 表示当天午夜。省略时区时使用 `.env` 中的默认值。
- `/help` 查看功能说明，仅调用者可见。

## 中文会议总结

1. 先加入一个普通语音频道，在文字频道运行 `/meeting start title:分享`。
2. Amadeus 公开说明记录状态，然后加入你所在的频道持续接收语音。它不会自己说话，不能设为耳聋。
3. 用 `/meeting status` 查看已接收语音包、音频段和本地转写进度。
4. 用 `/meeting stop` 停止记录并离开频道；等待后台转写完成后，中文总结自动发布到开始会议的文字频道。
5. 本地服务异常或 bot 重启后，可以用 `/meeting summary` 恢复最近一场已保存的会议，或指定 `meeting_id`。只能由发起者或有管理服务器权限的用户停止会议、重试总结。

会议总结按 Discord 发言者逐人列出，以一段自然语言概括各人的主要话题、观点、提问和回应。模型先结合相邻对话提取目标人的原话依据，再只根据这些依据写每人的概括。代码验证引用必须逐字来自该人的转写；标为不清楚的片段保存在依据文件中，不交给正文生成器推断含义；正文附待确认提示。生成后另行检查逐句支持、发言归属、否定与情绪是否反转。未通过时重写一次，再次失败则拒绝发布，保留录音供重试。模型核对仍可能判断错误，不等于人工确认。`summary.evidence.json` 保存每句概括的原话依据，方便人工审查。短交流也由本地模型概括，省略语气词和重复内容，不逐句复述、不自动套用决策或待办模板。模型引用的发言编号在内部校验，不展示在总结正文；完整原话和时间保存在 `transcript.md`，便于回听核对。人名、语音识别和模型概括仍可能出错。

长会议按每人最多约 4000 字符的输入批次提取原话，合并所有批次并按发言先后排列，再为每人生成一段整场归纳，合并重复话题并保留明确的观点修正；不再直接拼接多段小结。最终归纳后继续核对原话支持和归因。清楚的依据超过约 12000 字符时，分层筛选更有代表性的原话直到能放入本机模型上下文，避免提示词无限增长。筛选只允许选择原始编号，不改写原话或改变置信标记；原始提取依据、各层选择记录和最终句子的引用都保存在 `summary.evidence.json`。模型输出必须符合指定 JSON 结构；结构错误时修正一次，连续失败则停止总结。筛选仍可能遗漏信息，完整转写始终保留。

### 本地模型准备

本项目为 24GB Apple Silicon Mac 配置：**Qwen3-ASR 1.7B（MLX 4-bit）** 用 Metal GPU 转写；**Qwen3.5 9B（Ollama 原生 MLX，NVFP4）** 用 Metal 总结，优先理解文本而非速度；短发言直接概括，长段落开启思考。转写强制使用 Chinese，保留必要英文术语。模型来源：[Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)、[MLX 权重](https://huggingface.co/mlx-community/Qwen3-ASR-1.7B-4bit)。`ASR_MODEL` 配置转写模型，`MEETING_VOCABULARY` 可填写人名、项目名和专有名词，用逗号分隔。少于 2 秒的语音不带热词；识别结果包含至少 3 个配置术语时，用无热词提示重新转写，以核对是否发生词表回显。不会直接删除名字，也不能完全消除识别错误。

WebRTC VAD 先定位语音，合并短暂停顿并保留边界，再交给 Qwen；静音不送入转写模型。默认没有示例语句或术语提示。时间戳是语音段起点，不是逐词对齐。DEBUG 日志记录段起止时间、推理耗时和生成 token 数；不记录资源占用或功耗。术语表也可能造成提示词偏差，发现异常时先清空再回听录音。模型仍可能误识别，需核对原始 WAV。

VAD 定位后，RMS 低于 **−40 dBFS** 的语音片段跳过 ASR，减少极弱噪声被识别成文字。`ASR_MIN_RMS_DBFS` 可调整门槛，恰好等于门槛时保留。音量按单个带边界留白的 VAD 片段计算，不按整个 60 秒文件计算；原始 WAV 始终保留。DEBUG 记录跳过位置、RMS、峰值和门槛，以及每个文件跳过的数量。电平基于重采样后的波形，不能直接用于判断原始录音是否削波。

通过音量筛选的短回应保留在完整转写中；总结输入排除仅由嗯、啊、哦等语气词组成的发言，保留对、是、好、OK及含实质内容的句子。排除数量保存在 `summary.metrics.json` 并输出到日志。

首次安装（会下载本地模型权重，之后会议期间无需下载）：

```sh
brew install ollama opus
uv sync --locked
uv run amadeus-models
```

启动本机 Ollama，在一个终端保持运行：

```sh
OLLAMA_NO_CLOUD=1 OLLAMA_HOST=127.0.0.1:11434 ollama serve
```

在另一个终端下载总结模型并启动 bot：

```sh
ollama pull qwen3.5:9b
uv run amadeus
```

模型配置见 `.env.example`。`OLLAMA_URL` 只接受本机 loopback 地址，HTTP 不跟随重定向。音频不发送给外部转写服务；总结文本会发送到 Discord。此转写配置需要 Apple Silicon Mac 和 Metal，项目已固定 Python 3.12 或更新版本。

### 保存和恢复

每 60 秒按发言者保存一个 WAV 音频段，同时保存相对时间，分别转写后按时间排序。文件内仍由 VAD 按停顿划分语音片段。60 秒文件边界仍可能切断发言，但相比 30 秒边界次数更少；转写开始前最多等待约 60 秒。重叠发言不会先混成同一条音轨。长会议按发言者分段整理，合并时保留原始发言依据。所有本地文件位于被 Git 忽略的 `data/meetings/<会议ID>/`：音频、音频段清单、转写文本、会议元数据和 `summary.md`。只上传总结 Markdown，原始音频和完整转写留在本机。

新会议 ID 使用配置时区的开始时间，例如 `2026-10-07_00-25-30_UTC-0700`，目录、日志、Discord 状态和总结使用同一个 ID。UTC 偏移可区分夏令时；同一秒创建的会议自动追加 `_2`、`_3`，避免目录冲突。旧会议保留原目录和 ID，仍可通过 `/meeting summary meeting_id:旧ID` 恢复。

本机原始记录不会自动删除；不再需要时可删除对应会议目录。默认每场录音最多 2048MB，达到上限自动停止、保存记录并总结，可通过 `MAX_RECORDING_MB` 调整。停止时会保存最后一个不足 60 秒的音频段。处理失败保留音频，重试时跳过已成功转写的段。程序正常关闭时会保存音频段；突然断电可能丢失尚未关闭的 WAV 段，恢复只能使用已写入清单的音频。

Discord 语音接收使用支持 DAVE 的第三方扩展分支，依赖已固定到提交。库仍属实验性质；需要在真实 Discord 语音频道验证端到端接收和多人会议。重连后若语音接收中断，会停止并整理已收到的记录；重启后需要重新 `/meeting start` 才会继续收音。

## 本地运行

使用 [uv](https://docs.astral.sh/uv/) 管理 Python 和依赖，项目固定使用 Python 3.12。`uv.lock` 锁定依赖版本。

```sh
uv sync --locked
cp .env.example .env
```

在 `.env` 中填入 `DISCORD_TOKEN`。开发时建议填写 `DISCORD_GUILD_ID`，命令会立即同步到该服务器；留空则注册全局命令，Discord 客户端可能需要等待刷新。`DEFAULT_TIMEZONE` 使用 IANA 名称，例如 `America/Los_Angeles` 或 `Asia/Shanghai`。

```sh
uv run amadeus
```

`uv` 会自动管理 `.venv`，无需手动激活，也可以用 `uv run python -m amadeus` 启动。程序从当前目录查找 `.env`，请在仓库根目录运行。已有 `.env` 时不要重复复制模板。部署时可用 `uv sync --locked --no-dev` 仅安装运行依赖。

## Discord 配置

1. 在 [Discord Developer Portal](https://discord.com/developers/applications) 创建 Application，进入 Bot 页面获取 token，把 bot 名称设为 **Amadeus**。
2. 在 OAuth2 URL Generator 勾选 `bot` 和 `applications.commands`，邀请进服务器。
3. 给予 **View Channels、Send Messages、Embed Links、Send Polls、Attach Files** 权限，语音频道需要 **Connect**；线程中还需 **Send Messages in Threads**。无需 Administrator 或 Message Content Intent。
4. `.env` 只在本机保存，已被 Git 忽略；不要把 token 放进代码或聊天。若 token 泄漏，在 Portal 重置。

Bot 仅使用斜杠命令，不读取普通频道消息。两项创建命令限制为服务器内使用，并设置了每用户冷却时间。

## When2meet 集成

通过 [When2meet 官网](https://www.when2meet.com/) 当前使用的 `SaveNewEvent.php` 表单创建活动，支持服务器重定向和 HTML 中的链接/JavaScript 跳转。这不是有版本保证的公开 API；官网改版可能需要更新适配器。

创建请求有 20 秒超时，不自动重试，避免重复创建。失败信息仅调用者可见，并提供手动创建入口；成功后公开分享活动链接。链接公开到频道前，bot 会验证其域名和活动 ID 格式。如果创建成功但频道发送失败，会私下返回已创建的链接。

## 手动验证

### 本地进度和 debug 日志

无需重启 bot，开新终端查看本地会议文件进度（每 5 秒刷新，Ctrl+C 退出）：

```sh
uv run amadeus-progress
# 只查看一次
uv run amadeus-progress --once
```

显示最近五场会议的状态、音频大小、已转写/已封存音频段数量和文件更新时间。它只读本地文件，不启动第二个 bot。突然断电后旧元数据可能仍显示录音中，文件长时间不更新时需同时检查 bot 进程。

Bot 同时输出到启动终端和 `data/logs/amadeus.log`。默认 INFO 记录连接、收音、音频段保存、转写耗时、总结进度和异常栈。收音进度每 10 秒输出一次，包括语音包数、录音大小、已转写音频段数和待处理队列。语音包数持续为 0 时会提示检查发言及服务器耳聋状态。音频按 60 秒分段，因此转写日志不会每句话立即出现。

在仓库目录开一个新终端，实时查看（日志轮转后会继续跟随）：

```sh
tail -F data/logs/amadeus.log
```

查看详细诊断时，在 `.env` 设置 `LOG_LEVEL=DEBUG` 后重启 bot，或一次性启动：

```sh
LOG_LEVEL=DEBUG uv run amadeus
```

DEBUG 增加队列、已处理段跳过和模型 token 统计，不开启 Discord 协议原始包日志。日志不打印原始音频或转写正文，已知 bot token 会被替换为 `[REDACTED]`。日志每个文件最多 5MB，保留 3 个备份；日志位于 Git 忽略的 `data/` 内。

也可以在 Codex 中打开 `data/logs/amadeus.log`，或看启动 bot 的终端。不要在已有 bot 实例运行时再启动第二个实例；需要修改日志等级时先停止原进程。

```sh
uv run ruff check .
uv run ruff format --check amadeus
```

本项目不保留或添加单元测试。启动 bot 后，在 Discord 中验证 `/poll`、`/when2meet`，以及 `/meeting start` → 发言 → `/meeting status` → `/meeting stop` 的完整流程。确认语音包计数增加、本机音频和转写已保存、中文总结发布成功。服务异常时检查已保存的记录，并用 `/meeting summary` 重试。

`SUMMARY_THINKING=auto` 时，分段提取的目标发言或最终归纳的清楚原话超过 1500 字符才开启思考；超长会议的分层筛选也开启思考。短交流使用同一个 9B 模型直接概括；可设 `true` 或 `false` 强制开关。开启思考时允许 8192 个生成 token，否则 2048 个；32K 上下文、30 分钟超时。分析期间每 30 秒打印等待进度，正文只使用最终输出。日志显示分段提取进度、依据筛选层数和最终按人归纳阶段。`summary.metrics.json` 保存发言字符数、中文汉字数、各阶段请求耗时、整场耗时，以及每人的批次数、依据数量和筛选层数，不含提示词、姓名和时间戳的字数。思考内容不写入会议文件或日志。模型空闲 2 分钟后由 Ollama 卸载。

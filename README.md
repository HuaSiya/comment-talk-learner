# B站 + 贴吧学说话 / Comment Talk Learner

让麦麦每天从 **B站评论区** 和 **百度贴吧楼层** 学一点真人说话的方式：句式、语气、反问、自嘲、接话习惯，而不是学具体的梗。学到的内容写入麦麦原生的 **表达方式库**（`expressions` 表，`session_id=NULL` 即全局表达），由现有的表达选择机制在聊天时自然使用。

> 和 `B站学梗`/`贴吧学梗` 的区别：那两个插件学“一个梗是什么意思”，写入黑话库；本插件学“人怎么说话”，写入表达方式库。

## 功能

- 每天定时（默认北京时间 04:30，带随机抖动）自动跑一轮。
- **B站来源**：全站热门、分区排行、关键词搜索、指定 UP、固定 BV。
- **贴吧来源**：指定吧（默认生活吧）、可选关键词过滤、按回复数挑帖、读楼层回复。
- 只取评论/楼层，清洗掉 @、链接、表情码、广告/引流、纯语气词。
- 未登录时 B站 热门评论列表通常只返回 3 条，所以默认同时抓取楼中楼（每条热门评论最多 20 条回复），既补足样本，也更接近“人怎么接话”。
- 用模型抽取“情境 + 说话方式”，要求给出原文证据 id，并核对引号里的原话。
- 可选二次复核：过滤不可复用、脱离原视频/原帖听不懂、冒犯/引战/广告的内容。
- 与现有表达库做模糊去重，默认每天最多新增 8 条、累计最多 800 条。
- 支持手动触发、预览、状态、库查看和管理员回滚。

## 安装

- 插件市场搜索 **B站+贴吧学说话**，或插件 ID `github.husiya.comment-talk-learner`
- 手动安装：把仓库目录放到 `MaiBot/plugins/comment-talk-learner/`，重启 MaiBot
- 配置：复制 `config.example.toml` 为 `config.toml`，或在 WebUI 插件配置页修改

重启后日志出现 `B站+贴吧学说话插件已加载` 即成功。

> `config.toml` 留在本地，不要提交到仓库；升级插件时保留它。

## 命令

| 命令 | 说明 |
|---|---|
| `/学说话` | 立即跑一轮并自动入库（后台执行，完成后回包） |
| `/学说话预览` | 跑一轮但不写库，只回候选，适合先看效果 |
| `/学说话状态` | 今日运行、下次时间、配额、累计导入、最近错误 |
| `/学说话库` | 查看本插件最近导入的表达 |
| `/学说话回滚 确认` | 仅 `admin_users` 可用，删除本插件导入且未被人工改过的记录 |

建议第一次先用 `/学说话预览` 观察 1–2 轮，再开自动入库。

## 配置

`config.toml` 可以在 WebUI 的插件配置页改，也可以直接编辑后重启 core。

公共设置（`[plugin]`）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `write_to_db` | `true` | `false` = 只预览，不写表达库 |
| `daily_enabled` | `true` | 每日自动学习 |
| `daily_hour` / `daily_minute` | `4` / `30` | 北京时间执行时间 |
| `jitter_minutes` | `20` | 随机抖动，避免固定时间请求 |
| `run_on_start_if_missed` | `true` | 启动时已过点且今天没成功，是否补跑 |
| `max_expressions_per_day` | `8` | 每天最多新增表达（跨轮累计） |
| `max_total_imported` | `800` | 本插件累计导入上限 |
| `min_confidence` | `0.6` | 抽取置信度门槛 |
| `review_enabled` | `true` | 二次复核 |
| `review_min_score` | `0.65` | 复核分数门槛 |
| `llm_task` | `learner` | 复用麦麦已有的模型任务（下拉选择 learner/utils/planner 等） |
| `llm_fallback_task` | `utils` | 主任务输出为空/坏 JSON 时换任务重试 |
| `llm_batch_size` | `40` | 每次模型分析多少条评论/楼层 |
| `llm_timeout_seconds` | `180` | 单次模型调用超时；避免被 RPC 默认 30 秒超时打断 |
| `seen_video_ttl_days` | `14` | 同一视频/帖子多少天内不重复取样 |
| `max_retries_per_day` | `2` | 每天失败后最多重试几次 |
| `retry_interval_minutes` | `30` | 失败后隔多久重试 |
| `command_cooldown_seconds` | `300` | 手动命令冷却秒数 |
| `banned_words` | `[]` | 额外屏蔽词 |
| `admin_users` | `[]` | 允许回滚的用户 ID；留空禁用回滚 |

B站来源（`[sources]`）：

```toml
[sources]
popular_enabled = true
popular_pages = 1
zone_rids = []       # 游戏=4，动画=1，生活=160，知识=36，科技=188，鬼畜=119，美食=211
keywords = []        # 例如 ["大学生日常", "职场"]
up_uids = []         # 指定 UP 主 UID
bvids = []           # 固定补充 BV 号
per_source_limit = 5
max_video_age_days = 30

max_videos_per_run = 6
max_comments_per_video = 40
include_sub_replies = true
sub_reply_roots = 3
sub_replies_per_root = 20
min_reply_count = 50
min_comment_chars = 4
max_comment_chars = 120
rate_limit_seconds = 1.5
request_timeout = 20.0
max_request_retries = 2
sessdata = ""
```

来源合并是轮转的：指定 BV/UP/关键词/分区优先各取少量，再用热门补足，避免全站热门把指定来源挤掉。

贴吧来源：

```toml
[tieba]
enabled = true
bars = ["生活吧"]            # 会自动补“吧”
max_bars = 10                # 每轮最多处理前几个吧；超出的忽略
keywords = []                # 可选：只读标题/摘要包含这些词的帖子
scan_pages = 2               # 每个吧扫前几页帖子列表
max_threads = 0              # 贴吧总帖子数硬上限；0=不设上限
threads_per_bar = 2          # 每个吧最多读几个帖子
thread_pages = 1             # 每个帖子翻几页楼层
min_replies = 30             # 回复数低于该值不读
max_posts_per_thread = 30    # 每个帖子最多取多少层
min_post_chars = 6
max_post_chars = 300
include_op = false           # 默认只学回复楼层，不学楼主首楼
exclude_title = "水楼|签到|吧规|公告|广告|交易|招聘|投诉|申精|回复"
rate_limit_seconds = 2.0
cookie = ""                  # 贴吧 Cookie（BAIDUID/BDUSS/STOKEN），移动网页通道需要
proxy = ""                   # 数据中心 IP 被风控时，可填 HTTP 代理
transport = "auto"           # auto=客户端失败自动切移动网页；client；web
```

> 默认用“生活吧”是为了学日常接话；弱智吧、孙吧这类偏梗的吧容易抽出吧内黑话，插件会过滤“术语/黑话/梗/代指”类候选，但仍建议按你们群的氛围选吧。

多个吧时的取样规则：

- 每个吧最多读 `threads_per_bar` 个帖子（默认 2），所以 `bars` 填 3 个、`threads_per_bar=2` 时，一轮最多读 6 个帖子。
- `max_bars` 控制每轮实际处理 `bars` 里的前几个吧（默认 10，最多 20）；超出的吧本轮忽略。
- `max_threads` 是总上限；默认 `0` 表示不额外限制。如果设成 4，则 10 个吧总计最多 4 个帖子，后面的吧可能分不到。
- 吧按 `bars` 列表顺序处理；某个吧没有可用帖子时，会把名额留给后面的吧。
- **读取量提醒**：10 个吧 × 2 帖 = 最多 20 个帖子，约 20 页列表 + 20 页楼层请求；按 2 秒限速约 80 秒以上，再加模型抽取/复核。想控制耗时就把 `threads_per_bar` 调到 1，或用 `max_threads` 设总量上限。
- **内容风险**：孙笑川吧、抗压背锅吧、核战避难所吧、2ch 吧等偏攻击性/成人/边缘内容。插件会丢弃 `taboo=strong` 和“术语/黑话”候选，但仍建议先用 `/学说话预览` 看一轮，不合适就把这些吧从 `bars` 移除。

### 贴吧访问方式（重要）

实测：

- `c.tieba.baidu.com` 客户端 JSON 接口返回 `110001`（风控），没 Cookie 时 `tieba.baidu.com` 网页返回 `HTTP 403`。
- **带 Cookie 的移动网页版 `tieba.baidu.com/mo/q/m` 正常**，能拿到吧帖列表和楼层。
- 所以 `transport="auto"` 会先试客户端接口，遇到 `110001/403` 自动切移动网页；`transport="web"` 可强制只走网页。

需要准备一个贴吧 Cookie（`BAIDUID`/`BDUSS`/`STOKEN`），建议小号。没有 Cookie 或代理时，贴吧来源会记录警告并跳过，B站来源仍然正常工作。

用调试脚本可以先测通不通：

```bash
docker exec -i maim-bot-core python \
  /MaiMBot/plugins/comment-talk-learner/tools/dry_run_tieba.py \
  --bar 生活吧 --pages 1 --threads 1 --posts 10
```

### 实测情况

- B站与贴吧来源均已在 MaiBot 1.3.18 上跑通；贴吧在数据中心 IP 下通常需要 Cookie 或代理。
- 单元测试覆盖客户端接口解析、移动网页解析、选帖、楼层过滤、去重、状态、调度和旧配置迁移。

## 工作流程

```
B站热门/排行/搜索/UP/BV        贴吧吧帖/楼层
          \                    /
           视频评论 + 楼中楼 + 楼层回复
                    ↓
           清洗 + 过滤 + 全轮去重
                    ↓
   模型抽取：情境 + 说话方式 + 证据 id + 置信度 + 敏感度
                    ↓
           核对证据 id / 引号原话
                    ↓
       可选二次复核（可复用、脱离原帖能懂、不冒犯）
                    ↓
       与现有全局表达模糊去重 + 每日/累计配额
                    ↓
       写入 expressions（session_id=NULL, checked=False）
                    ↓
       麦麦原生表达选择机制在聊天时使用
```

## 可观测与验证

```bash
# 插件是否加载
docker compose logs --tail=200 core | grep -i "评论区学说话"

# 表达库入库前后数量
sqlite3 data/MaiMBot/MaiBot.db "select count(*) from expressions where session_id is null;"
sqlite3 data/MaiMBot/MaiBot.db "select id,situation,style from expressions where session_id is null order by id desc limit 10;"
```

只抓取、不写库的调试脚本（在容器里跑）：

```bash
docker exec -i maim-bot-core python \
  /MaiMBot/plugins/comment-talk-learner/tools/dry_run.py \
  --videos 2 --comments 20 --show-prompt
```

单元测试（容器里）：

```bash
docker exec -i maim-bot-core python -m unittest discover \
  -s /MaiMBot/plugins/comment-talk-learner/tests -v
```

## 注意事项与边界

- **B站风控**：默认 1.5 秒限速、单轮少量请求；遇到 `-352`/HTTP 412 会停止本轮并留待下次。`sessdata` 可选，不保证长期稳定；不要用主号。
- **贴吧风控**：数据中心 IP 常见 `110001`/HTTP 403；需要贴吧 Cookie 或代理，详见上面的“贴吧访问限制”。贴吧失败不会影响 B站来源。
- **自动入库会污染语气**：靠每日 8 条配额、二次复核、证据校验、模糊去重、敏感度过滤和回滚兜底。仍建议先用 `/学说话预览` 观察几天。配额是“每天跨轮累计”，手动补跑不会突破当日上限。
- **热评偏差**：热评偏梗和情绪表达，不完全是日常说话；用关键词/分区/UP 补充日常样本。
- **表达生效是概率性的**：新表达进入全局库后由原生选择机制抽取，不保证下一句立刻使用。
- **人工审核标记**：写入的是 `checked=False`。如果以后开启 `[expression] expression_checked_only = true`，这些记录不会被使用，除非在 WebUI 里人工审核。
- **向量索引**：直接写库不会立刻更新表达向量索引；如果开启 `use_vector_expression`，宿主的历史回填任务会后续索引，不影响本轮入库。
- **回滚**：只会删除 `daily_state.json` 中记录、且 style/situation 仍与快照一致的记录；人工改过或非本插件创建的记录不会动。
- **状态文件**：位于插件的 `ctx.paths.data_dir/daily_state.json`，请保留；删除会丢失“已看视频/已导入”记录，可能重复学习。
- 插件不读取视频画面，也不保证模型抽取完整或准确；它是“学习素材”，不是事实来源。

## 排错

- **命令没反应**：确认 `enabled=true`，并重启 core；中文命令需要 `/` 前缀。
- **一直失败**：看日志里的 B站错误；`-352` 等几分钟或调大 `rate_limit_seconds`；关键词/UP 来源失败可能只是 WBI 接口临时风控，热门仍可用。
- **`cap.call 超时 (30000ms)` / 模型没有返回任何有效候选**：插件已用 `llm_timeout_seconds=180` 覆盖 RPC 默认 30 秒。如果仍失败，调大 `llm_timeout_seconds`、把 `llm_batch_size` 降到 20、选更快的 `llm_task`（如 `utils`/`fast_model`），或减少 `threads_per_bar`/`max_threads` 缩短单轮耗时。
- **学不到东西**：热门视频评论数可能低于 `min_reply_count`，或候选都被复核/去重过滤；用 `/学说话预览` 看是哪一步。
- **表达太奇怪**：先把 `write_to_db=false` 或 `daily_enabled=false`，用预览观察；必要时用 `/学说话回滚 确认`（先配置 `admin_users`）清掉本插件记录，再调低 `max_expressions_per_day` 或提高 `min_confidence`。
- **想只学某个圈子**：把 `popular_enabled=false`，配置 `zone_rids`/`keywords`/`up_uids`/`bvids`。

## 许可

MIT

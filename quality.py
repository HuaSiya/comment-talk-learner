"""B站评论质量过滤：剔除公式化套话、玩梗、标题党和无意义评论。

这里的规则只用于 B站评论取样（``formulaic_filter=true``）；贴吧暂不启用
严格过滤。但 ``is_formulaic_expression`` 会在候选校验阶段对所有来源生效，
避免模型把“火钳刘明”“下次一定”这类固定说法当成可复用的说话方式。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Iterable

_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_QUOTE_RE = re.compile(r"[「『“\"]([^」』”\"]{1,40})[」』”\"]")

# 一眼就能认出的 B站评论区套话/梗。全部用 compact_text 形式书写，
# 命中即整条丢弃，不再进入模型抽取。
_HARD_TERMS = (
    # 抢前排 / 怀旧 / 玩梗
    "火钳刘明",
    "火前留名",
    "火钳留名",
    "火前刘明",
    "打破惨案",
    "惨案",
    "空耳",
    "计数君",
    "下次一定",
    "意满离",
    "爷青回",
    "爷青结",
    "时代的眼泪",
    "泪目",
    "破防",
    "能者先阅",
    "前排打卡",
    "前排留名",
    "前排围观",
    "前排出售",
    "热评密码",
    "流量密码",
    "热评预定",
    "热评",
    "禁止套娃",
    "套娃",
    "前方高能",
    "非战斗人员",
    "这么可爱一定是男孩子",
    "这么可爱一定是女孩",
    "失踪人口回归",
    "诈尸",
    "爷爷你关注的",
    "等了好久终于",
    # 废话文学 / 固定句式
    "听君一席话",
    "胜似一席话",
    "但凡有一点意义",
    "一分钟等于六十秒",
    "中国有句老话",
    # 吐槽 / 求科普模板
    "有没有一种可能",
    "很难不怀疑",
    "精准戳中",
    "有没有人和我一样",
    "有没有大佬",
    "有没有相关资料",
    "收藏有空",
    "建议反复观看",
    "反复观看",
    # 音乐 / 剪辑区
    "这个bgm绝了",
    "bgm绝了",
    "循环预定",
    "耳朵怀孕",
    "卡点舒服",
    # 2025 年 B站固定梗
    "lookinmyeyes",
    "tellmewhy",
    "鸣潮公式",
    "秧式新文",
    "秧式",
    "a是这样的",
    "要考虑的事就很多了",
    "全民制作人",
    "说点大家不想的",
    "孙笑川",
    "小米型",
    "蔚来型",
    "比亚迪型",
    "特斯拉型",
    "问界型",
    "小鹏型",
    # 标题党 / 夸张话术
    "冒死上传",
    "价值百万",
    "内部员工泄露",
    "内部员工",
    "绝密资料",
    "不要笑挑战",
    "人类迷惑行为大赏",
    "人类迷惑行为",
    "小学生逆天作业",
    "颠覆三观",
    "毁三观",
    "智商税",
    "爆肝",
    "逐帧分析",
    # 捧场 / 追更
    "宝藏up",
    "这么高质量",
    "为什么没人看",
    "你火了",
    "看好你",
    "期待下一期",
    "关注了",
    "已关注",
    "关注一波",
    "百大",
    "学一辈子",
    "鲨疯了",
    "杀疯了",
    "终于有脸了",
    "小时候的回忆",
    "童年回忆",
    "终于找到原版",
    "很难再看到这种作品",
    "在为什么你能发语音",
    "讲个鬼故事",
    "看进度条",
    "感谢up科普",
    "别的不说这点",
    # 常见 B站评论区梗
    "细糠",
    "申遗",
    "传下去",
    "格局打开",
    "格局小了",
    "电子榨菜",
    "电子宠物",
    "dna动了",
    # 常见 B站梗词
    "yyds",
    "awsl",
    "xswl",
    "绝绝子",
    "蚌埠住了",
    "绷不住了",
    "笑不活了",
    "栓q",
    "退退退",
    "鸡你太美",
    "小黑子",
    "一眼丁真",
    "丁真",
    "坤坤",
    "ikun",
    "原神启动",
    "赢麻了",
    "输麻了",
    "典中典",
)

# 贴吧特有的水楼、互粉、资源/交易模板。参考 TiebaCleaner / TiebaTS /
# RefreshingTieba / tieba-keyword-filter / tieba-enhancer 等脚本的过滤思路：
# 它们大多把“广告/水贴/引流”做成关键词 + 模板规则，这里取可离线判定的部分。
_TIEBA_HARD_TERMS = (
    # 水经验 / 十五字 / 顶帖
    "十五字",
    "凑够十五字",
    "水经验",
    "混经验",
    "蹭经验",
    "水一贴",
    "水一波",
    "水一水",
    "水贴",
    "水楼",
    "盖楼",
    "抢楼",
    "占楼",
    "前排",
    "沙发",
    "板凳",
    "地板",
    "地下室",
    "下水道",
    "围观",
    "路过",
    "打酱油",
    "留名",
    "马克",
    "make",
    "mark",
    "刘明",
    "火钳刘明",
    "帮顶",
    "顶帖",
    "顶一个",
    "支持一下",
    "回复可见",
    "隐藏内容",
    "下载留名",
    # 感谢 / 好人 / 旧贴吧梗
    "楼主好人",
    "一生平安",
    "好人一生平安",
    "1024",
    "感谢分享",
    "谢谢楼主",
    "楼主辛苦了",
    "学习了",
    "收藏了",
    "先马后看",
    "马住",
    "码住",
    "留个脚印",
    "眼熟我",
    "混脸熟",
    "不明觉厉",
    "喜大普奔",
    "人艰不拆",
    "细思恐极",
    "男默女泪",
    "十动然拒",
    "累觉不爱",
    "图样图森破",
    "元芳你怎么看",
    "我和小伙伴们都惊呆了",
    "高端大气上档次",
    "低调奢华有内涵",
    "待我长发及腰",
    "你家里人知道吗",
    "土豪我们做朋友吧",
    "何弃疗",
    "感觉不会再爱了",
    "我读书少你别骗我",
    "哪家强",
    "挖掘机技术哪家强",
    "中国山东找蓝翔",
    "一百块都不给我",
    "我从未见过如此厚颜无耻之人",
    "城会玩",
    "乡话多",
    "理都懂",
    "然并卵",
    "睡起嗨",
    "说闹觉余",
    # 互粉 / 引流 / 资源
    "求粉",
    "互粉",
    "互关",
    "已粉",
    "回粉",
    "看头像",
    "看主页",
    "加群",
    "群号",
    "群文件",
    "资源君",
    "网盘",
    "提取码",
    "解压密码",
    "留邮箱",
    "发邮箱",
    "求资源",
    "求链接",
    "求种",
    "种子",
    "磁力",
    "ed2k",
    "迅雷",
    "百度云",
    "阿里云盘",
    "夸克网盘",
    "uc网盘",
    "天翼云盘",
    "老司机",
    "开车",
    "上车",
    "车牌",
    # 交易 / 招聘 / 代练 / 微商
    "走闲鱼",
    "闲鱼",
    "转转",
    "微店",
    "代购",
    "微商",
    "刷单",
    "日结",
    "底薪",
    "提成",
    "包吃住",
    "代练",
    "代打",
    "代肝",
    "代抽",
    "代充",
    "刷好评",
    "刷赞",
    "刷粉",
    "刷等级",
    "网赚",
    "副业",
    "收徒",
)

_TIEBA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^(?:前排|二楼|三楼|沙发|板凳|地板|地下室|下水道|占楼|盖楼|抢楼|"
            r"围观|路过|打酱油|留名|马克|make|mark|刘明|帮顶|顶|顶帖|支持一下|"
            r"学习了|收藏了|先马后看|马住|码住)$"
        ),
        "贴吧水楼/顶帖",
    ),
    (re.compile(r"(?:十五字|凑够十五字|经验[+＋加]?\d+|水(?:一贴|一波|一水|经验|贴|楼))"), "贴吧水经验"),
    (
        re.compile(r"(?:楼主好人|好人一生平安|一生平安|1024|感谢分享|谢谢楼主|楼主辛苦了)"),
        "贴吧感谢模板",
    ),
    (re.compile(r"(?:求粉|互粉|互关|已粉|回粉|眼熟我|混脸熟)"), "贴吧互粉"),
    (
        re.compile(
            r"(?:看头像|看主页|加群|群号|群文件|资源君|网盘|提取码|解压密码|"
            r"留邮箱|发邮箱|求资源|求链接|求种|种子|磁力|ed2k|迅雷|百度云|"
            r"阿里云盘|夸克网盘|uc网盘|天翼云盘|老司机|开车|上车|车牌)"
        ),
        "贴吧资源/引流",
    ),
    (re.compile(r"(?:出售|收购|求购|交易).{0,8}(?:账号|物品|资源|价格|私信|平台|链接)"), "贴吧交易"),
    (re.compile(r"(?:兼职|招聘|日结|底薪|提成|包吃住).{0,8}(?:联系|私信|加|工资|元)"), "贴吧招聘"),
    (re.compile(r"(?:淘宝|拼多多|微店|闲鱼|转转).{0,8}(?:店铺|链接|优惠|下单|搜索)"), "贴吧导购"),
)

# 模板句式；与 _HARD_TERMS 不同，这些用正则匹配结构，覆盖“X 年/天”等变量。
_FORMULA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^(?:这|那)?(?:也|真|可|实|确|好)?太(?:强|牛|棒|秀|绝|神|帅|美|可爱|厉害|优秀)[了啦吧啊呀哦哈]{0,2}$"),
        "泛化捧场",
    ),
    (re.compile(r"^(?:今天|今日|每天|日常|第一次|第[0-9一二三四五六七八九十百千万两]+次)?(?:打卡|签到|来了|来啦|留名|考古|围观|已阅)$"), "打卡/签到"),
    (
        re.compile(r"^(?:前排|沙发|板凳|地板|[二三四五六七八九十]楼|占楼|挤挤|第一次|第一)(?:[啊呀呢吧哦哈嘛啦的了太很真的属实简直也又都就我你他她它这那0-9]*)$"),
        "前排/楼层",
    ),
    (re.compile(r"^(?:热乎的?|还热乎|刚出锅|新鲜出炉)$"), "抢热乎"),
    (re.compile(r"(?:up|up主|阿婆主|阿婆).{0,10}(?:加油|厉害|高质量|百大|关注|期待|支持|宝藏|粉了|爱了|不错|优秀|好看|好听|好帅|好美|好可爱|好厉害|可爱|帅|美|牛|棒|强)"), "UP捧场"),
    (re.compile(r"叫.{0,12}进来学(?:学|一学)"), "叫XX进来学"),
    (re.compile(r"(?:现在|以前).{0,12}(?:vs|对比|比).{0,12}(?:现在|以前)"), "新旧对比"),
    (
        re.compile(r"(?:耗时|爆肝|肝了|做了|研究了)[0-9一二三四五六七八九十百千万两]+(?:年|个月|月|天|小时|分钟)"),
        "标题党耗时",
    ),
    (re.compile(r"(?:冒死|拼死).{0,6}(?:上传|分享|发布)"), "冒死上传"),
    (re.compile(r"(?:价值百万|价值百万的).{0,8}(?:资料|教程|课程|秘密)"), "价值百万"),
    (re.compile(r"(?:内部员工|内部人员).{0,6}(?:泄露|流出|资料|消息)"), "内部泄露"),
    (re.compile(r"(?:不要笑挑战|人类迷惑行为|迷惑行为大赏|逆天作业|小学生逆天)"), "挑战/迷惑行为"),
    (re.compile(r"(?:别再|不要再).{0,12}(?:99%|百分之九十九|做错|踩坑|交智商税|都错了)"), "警告式标题"),
    (
        re.compile(r"(?:99%|99％|百分之九十九).{0,10}(?:人|都).{0,10}(?:做错|不知道|踩坑|错了|交智商税)"),
        "99%标题党",
    ),
    (re.compile(r"(?:颠覆三观|毁三观|三观尽毁|智商税|智商检测)"), "颠覆三观/智商税"),
    (re.compile(r"这才(?:是|叫).{0,16}该有的样子"), "怀旧吹捧"),
    (re.compile(r"回答我.{0,10}(?:看我的眼睛|看着我的眼睛|眼睛)"), "回答我质问体"),
    (re.compile(r"回答我(?:啊|呀|吧)?$"), "回答我质问体"),
    (re.compile(r"(?:小米|蔚来|比亚迪|特斯拉|问界|小鹏)型"), "理想型谐音"),
    (re.compile(r"(?:这个?bgm|配乐|音乐).{0,6}(?:绝了|太绝|神了|好评|舒服)"), "BGM套话"),
    (re.compile(r"(?:循环|单曲循环).{0,4}(?:预定|安排|走起)"), "循环预定"),
    (re.compile(r"耳朵.{0,2}怀孕"), "耳朵怀孕"),
    (re.compile(r"(?:卡点|踩点).{0,4}(?:舒服|绝了|爽|完美)"), "卡点套话"),
    (re.compile(r"(?:有没有大佬|大佬).{0,8}(?:讲讲|总结|科普|解释|链接)"), "求科普模板"),
    (re.compile(r"(?:收藏|码住|马克).{0,6}(?:有空|慢慢看|以后看|回头看)"), "收藏模板"),
    (
        re.compile(r"(?:学到|涨知识).{0,8}(?:感谢|谢谢).{0,8}(?:up|up主|阿婆主|科普|分享)"),
        "感谢科普模板",
    ),
    (re.compile(r"(?:太|好|很|真|有点|过于|纯)抽象(?:了|吧|啊|呀)?"), "抽象梗"),
    (re.compile(r"(?:太|好|很|真|有点|过于|纯)逆天(?:了|吧|啊|呀)?"), "逆天梗"),
    (re.compile(r"(?:热评|弹幕|评论区).{0,6}(?:密码|预定|第一|前排)"), "热评/评论区套话"),
)

# 只在“整条评论很短”时才按梗词丢弃，避免误伤正常长句。
_MEME_TERMS = ("绷不住", "蚌埠住", "笑不活", "电棍", "otto")

# 纯捧场/情绪词的组合；只有整条评论几乎全由这些词组成时才丢。
_GENERIC_TOKENS = (
    "好看",
    "好听",
    "好帅",
    "好美",
    "好可爱",
    "太棒了",
    "太强了",
    "太厉害了",
    "太绝了",
    "绝了",
    "神了",
    "爱了",
    "喜欢",
    "支持",
    "支持一下",
    "加油",
    "感动",
    "泪目",
    "破防",
    "笑死",
    "哈哈",
    "草",
    "awsl",
    "xswl",
    "yyds",
    "666",
    "233",
    "赞",
    "顶",
    "好耶",
    "可以",
    "不错",
    "优秀",
    "高质量",
    "宝藏",
    "爱看",
    "多看",
    "再来",
    "催更",
    "更新",
    "蹲",
    "码住",
    "收藏",
    "学到了",
    "感谢",
    "谢谢",
    "确实",
    "好",
    "妙",
    "强",
    "棒",
    "秀",
    "精彩",
    "完美",
    "舒服",
    "爽",
    "爱",
    "关注",
    "已关注",
    "关注了",
    "关注一波",
    "三连",
    "一键三连",
    "投币",
    "点赞",
    "点个赞",
    "up",
    "up主",
    "阿婆主",
    "哇",
    "厉害",
    "牛",
    "牛逼",
    "神",
    "无敌",
    "顶级",
    "天花板",
    "笑不活了",
    "蚌埠住了",
    "绷不住了",
    "绝绝子",
    "栓q",
    "退退退",
    "典",
    "麻了",
    "赢麻了",
    "输麻了",
)
_GENERIC_RE = re.compile(
    r"(?:" + "|".join(re.escape(token) for token in sorted(set(_GENERIC_TOKENS), key=len, reverse=True)) + r")+"
)

# 泛化夸奖组合，例如“好牛的质量”“这个视频的剧情很好”，没有具体观点。
_PRAISE_ONLY_RE = re.compile(
    r"^(?:"
    r"这|那|个|是|的|了|啊|呀|吧|呢|嘛|啦|哈|哦|我|你|真|太|很|非常|特别|相当|"
    r"好|牛|棒|强|绝|神|厉害|优秀|高质量|好看|好听|好帅|好美|好可爱|顶级|无敌|天花板|"
    r"质量|水平|操作|作品|视频|内容|画面|剪辑|配乐|bgm|声音|颜值|创意|细节|节奏|剧情|"
    r"演技|唱功|舞技|制作|演出|舞台|设计|构图|配色|文案|选题|脑洞|整活|丝滑|舒服|震撼|惊艳"
    r")+$"
)

# 打卡/前排/楼层类的短评论，整条都是这些词才算命中。
_POSITION_TOKEN = (
    r"(?:第[0-9一二三四五六七八九十百千万两]+|第一次|前排|沙发|板凳|地板|"
    r"[二三四五六七八九十]楼|占楼|挤挤|打卡|签到|考古|留名|围观|来了|来啦|"
    r"热乎的?|还热乎|刚出锅|新鲜出炉|已阅|能者先阅|顶|赞|支持一下|加油|"
    r"前排打卡|前排留名|前排围观|前排出售)"
)
_POSITION_RE = re.compile(rf"{_POSITION_TOKEN}+")
_REPEAT_RE = re.compile(r"(.{1,6})\1{2,}")
_NUMERIC_RE = re.compile(r"\d{2,}")

# 候选表达里出现这些词，说明它在描述“评论区/平台行为”，不是日常说话方式。
_META_STYLE_RE = re.compile(
    r"(?:捧场|打卡|前排|刷屏|刷弹幕|热评|三连|一键三连|催更|玩梗|接梗|造梗|复读|"
    r"弹幕|评论区|b站|up主|阿婆主|up(?![a-z])|吧友|楼主|标题党|引流|套话|公式)"
)

# ---------------- B站评论质量 v2：账号 / AI / 广告 / 质量分 ----------------

# 纯反应：只表达情绪或语气，不包含具体观点。按比例少量保留，避免语料变成客服话术。
_PURE_REACTION_RE = re.compile(
    r"^(?:哈+|6+|草+|笑死(?:我了|hhh)?|awsl|xswl|好耶|绝了|神了|牛+|啊+|"
    r"太(?:强|牛|棒|秀)了?|爱了|泪目|破防|绷不住了|蚌埠住了|笑不活了|"
    r"典|麻了|赢麻了|输麻了)[啊呀呢吧哦哈嘛啦的了太很真的属实简直也又都就我你他她它这那！!。.，,~～\s]*$"
)

# 广告、引流、联系方式。URL 在 sanitize_comment 里已经去掉，这里覆盖文字话术。
_AD_RE = re.compile(
    r"(?:https?://|www\.|加v|加微信|微信号|vx|qq群|qq号|私信|领券|返利|优惠券|福利群|"
    r"兼职|刷单|代练|看主页|看头像|带货|下单|淘口令|拼多多|淘宝|京东|小店|橱窗|公众号|扫码|"
    r"二维码|手机号|邮箱|联系电话|b站搜|b站搜索|搜索up主|关注up主|关注博主|主页链接|"
    r"简介链接|评论区链接|置顶链接|点击链接|传送门|加群|群号|群文件|资源君|网盘|提取码|"
    r"解压密码|留邮箱|发邮箱|求种|种子|磁力|ed2k|迅雷|百度云|阿里云盘|夸克网盘|uc网盘|"
    r"天翼云盘|闲鱼|转转|微店|代购|微商|日结|底薪|提成|包吃住|代打|代肝|代抽|代充|"
    r"刷好评|刷赞|刷粉|刷等级|网赚|副业|收徒)",
    re.IGNORECASE,
)

# 严重辱骂、隐私和人肉信息；普通吐槽/玩梗不在这里处理。
_TOXIC_RE = re.compile(
    r"(?:nmsl|死全家|去死|杀了你|傻逼|脑残|智障|畜生|杂种|人肉|开盒|"
    r"身份证|住址|裸照|约炮|嫖娼|强奸|轮奸|肢解|直播自杀)",
    re.IGNORECASE,
)

# 明确的 AI 元讨论/自报身份；这类评论容易教机器人自称 AI，直接丢弃。
_AI_META_RE = re.compile(
    r"(?:我是|作为|本)(?:一个)?(?:ai|人工智能|机器人|人机|智能助手)|"
    r"(?:大肥鱼|本鱼|本鲸|deepseek|chatgpt|gpt|kimi|通义|豆包|文心一言|人工智能|机器人|人机|"
    r"召唤ai|让ai|ai总结|ai课代表|ai小助手|ai字幕|ai分析)",
    re.IGNORECASE,
)

# AI 评论模板家族：主人汇报、视频摘要、建议清单、评价、角色自称。
_AI_FAMILIES = (
    re.compile(r"(?:主人|汇报|为您|我已经|本鱼|本鲸)"),
    re.compile(r"(?:视频|UP主|up主|这期|本期).{0,10}(?:讲|说|介绍|展示|内容|摘要|总结)"),
    re.compile(r"(?:建议|推荐|可以试试|不妨|最好|应该|步骤|首先|其次|最后)"),
    re.compile(r"(?:优点|缺点|亮点|不足|适合人群|总体来说|综合评价|评价)"),
    re.compile(r"(?:我是AI|作为AI|作为一个人工智能|本AI|AI助手|人工智能)"),
)
_AI_STRUCTURE_RE = re.compile(
    r"(?:首先|其次|再次|最后|综上所述|总体来说|总的来说|希望对你有帮助|"
    r"亮点|不足|适合人群|第一步|第二步|第三步|1[.、]|2[.、]|3[.、])"
)

# 已知的总结/课代表类账号名；命中直接高分。
_KNOWN_BOT_NAMES = (
    "AI视频小助理",
    "课代表猫",
    "AI全文总结",
    "AI课代表",
    "AI总结",
    "AI助手",
    "视频总结",
    "内容总结",
    "智能总结",
    "AI字幕",
    "AI看片",
    "AI影视",
    "AI观察",
    "AI分析",
    "AI解读",
    "自动总结",
    "摘要助手",
    "笔记助手",
    "视频小助手",
    "B站课代表",
    "课代表",
    "小助理",
    "贴吧机器人",
    "自动回复",
    "资源君",
    "分享君",
    "搬运工",
    "福利君",
    "资源库",
)
_BOT_NAME_RE = re.compile(
    r"(?<![A-Za-z])(?:ai|gpt|chatgpt|deepseek|kimi|claude|bot)(?![A-Za-z])|"
    r"(?:助手|课代表|总结|摘要|小助理|导航员|解说员|字幕组|"
    r"机器人|自动回复|资源君|分享君|搬运工|代练|代购|微商|刷单|兼职|"
    r"广告|推广|交流群|qq群|群主|管理员|官方客服|小编|吧务)",
    re.IGNORECASE,
)

_PERSONAL_RE = re.compile(r"(?:我|我们|咱|昨天|今天|前天|当时|后来|结果|记得|有一次|上次|朋友|同事|家里|公司|学校|路上|我妈|我爸)")
_DETAIL_RE = re.compile(r"(?:\d|[一二三四五六七八九十百千万两]+(?:年|天|小时|分钟|块|岁|次|个))")
_OPINION_RE = re.compile(r"(?:觉得|认为|说实话|其实|反正|真的|确实|离谱|服了|无语|好家伙|难受|开心|生气|失望|惊喜)")
_INTERACTION_RE = re.compile(r"(?:[？?]|吗|呢|吧|啊|呀|嘛|呗|哈|啦)")
_TEMPLATE_RE = re.compile(
    r"(?:首先|其次|再次|最后|综上所述|总体来说|总的来说|希望对你有帮助|"
    r"亮点|不足|适合人群|建议|第一步|第二步|第三步)"
)
_RESTATE_RE = re.compile(r"(?:视频|UP主|up主|这期|本期|内容).{0,8}(?:讲|说|介绍|展示|提到|总结|摘要)")


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_pure_reaction(text: str) -> bool:
    """整条评论只是“哈哈/草/666/awsl/太强了”这类情绪反应。"""
    compact = compact_text(text)
    return bool(compact and _PURE_REACTION_RE.fullmatch(compact))


def reaction_sample_value(text: str) -> float:
    """稳定地给纯反应打 0~1 的采样值，保证同一评论多次运行结果一致。"""
    digest = hashlib.sha1(str(text or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


def should_keep_reaction(text: str, ratio: float) -> bool:
    """按比例保留纯反应；ratio=0 全丢，ratio=1 全留。"""
    ratio = max(0.0, min(1.0, float(ratio or 0)))
    if ratio <= 0:
        return False
    if ratio >= 1:
        return True
    return reaction_sample_value(text) < ratio


def chinese_ratio(text: str) -> float:
    """中文字符占中文/英文/数字的比例，用来过滤非中文语料。"""
    raw = str(text or "")
    cjk = len(_CJK_RE.findall(raw))
    latin = len(re.findall(r"[A-Za-z]", raw))
    digits = len(re.findall(r"\d", raw))
    total = cjk + latin + digits
    return cjk / total if total else 0.0


def is_ad_or_lead(text: str) -> tuple[bool, str]:
    raw = str(text or "")
    match = _AD_RE.search(raw) or _AD_RE.search(compact_text(raw))
    if match:
        return True, f"广告/引流：{match.group(0)}"
    return False, ""


def is_toxic_or_private(text: str) -> tuple[bool, str]:
    raw = str(text or "")
    match = _TOXIC_RE.search(raw) or _TOXIC_RE.search(compact_text(raw))
    if match:
        return True, f"辱骂/隐私：{match.group(0)}"
    return False, ""


def is_ai_meta(text: str) -> tuple[bool, str]:
    raw = str(text or "")
    match = _AI_META_RE.search(raw) or _AI_META_RE.search(compact_text(raw))
    if match:
        return True, f"AI元讨论：{match.group(0)}"
    return False, ""


def bot_account_score(
    *,
    user: str = "",
    level: int | None = None,
    sign: str | None = None,
    avatar: str | None = None,
    vip: int | None = None,
) -> int:
    """按用户名、等级、签名、头像、会员状态给账号打 Bot 风险分。

    None 表示该字段未知（例如贴吧网页楼层拿不到等级/头像），不要按
    “低等级/默认头像”扣分，否则会误伤正常吧友。
    """
    name = str(user or "").strip()
    lower = name.lower()
    level_known = level is not None
    sign_known = sign is not None
    avatar_known = avatar is not None
    vip_known = vip is not None
    level_value = _as_int(level)
    vip_value = _as_int(vip)
    sign_text = str(sign or "").strip()
    avatar_text = str(avatar or "").lower()
    if not (name or level_known or sign_known or avatar_known or vip_known):
        return 0
    score = 0
    if any(term in lower for term in ("大肥鱼", "本鱼", "本鲸")):
        score += 50
    elif any(term in lower for term in ("鲸鱼", "肥鱼")):
        # “鲸鱼/肥鱼”也可能是正常网名，先给弱信号；配合低等级/默认头像再判。
        score += 20
    if any(term.lower() in lower for term in _KNOWN_BOT_NAMES):
        score += 60
    if _BOT_NAME_RE.search(name):
        score += 40
    if level_known:
        if level_value <= 1:
            score += 20
        elif level_value <= 2:
            score += 15
    if sign_known and not sign_text:
        score += 5
    if avatar_known and (not avatar_text or "noface" in avatar_text):
        score += 15
    if vip_known and vip_value > 0:
        score -= 10
    return max(0, score)


def ai_text_score(text: str) -> int:
    """按模板家族、结构骨架和 AI 自报身份给文本打风险分。"""
    raw = str(text or "")
    families = sum(1 for pattern in _AI_FAMILIES if pattern.search(raw))
    structures = {match.group(0) for match in _AI_STRUCTURE_RE.finditer(raw)}
    score = families * 20 + min(len(structures), 4) * 10
    if _AI_META_RE.search(raw):
        score += 50
    if re.search(r"(?:主人|本鱼|本鲸|大肥鱼)", raw):
        score += 30
    return score


def is_bot_or_ai_comment(
    text: str,
    meta: dict[str, Any] | None = None,
    *,
    threshold: int = 60,
) -> tuple[bool, str, int]:
    """综合账号特征和文本模板，判断是否是 AI/Bot 评论。"""
    info = meta or {}
    account = bot_account_score(
        user=str(info.get("user") or ""),
        level=info.get("level"),
        sign=info.get("sign"),
        avatar=info.get("avatar"),
        vip=info.get("vip"),
    )
    text_score = ai_text_score(text)
    total = account + text_score
    meta_hit, meta_reason = is_ai_meta(text)
    if meta_hit:
        return True, meta_reason, max(total, int(threshold))
    if total >= max(1, int(threshold)):
        return True, f"AI/Bot 风险分 {total}", total
    return False, "", total


def comment_quality_score(text: str, *, like: int = 0, reply_count: int = 0) -> int:
    """给评论打质量分，用于优先把有细节/有观点的素材喂给模型。"""
    raw = str(text or "")
    compact = compact_text(raw)
    if not compact:
        return -5
    score = 0
    if _PERSONAL_RE.search(raw):
        score += 2
    if len(raw) >= 15:
        score += 1
    if _DETAIL_RE.search(raw):
        score += 1
    if _OPINION_RE.search(raw):
        score += 1
    if _INTERACTION_RE.search(raw):
        score += 1
    if _as_int(like) >= 50:
        score += 1
    if _as_int(reply_count) >= 5:
        score += 1
    if _TEMPLATE_RE.search(raw):
        score -= 2
    if _RESTATE_RE.search(raw):
        score -= 2
    if is_pure_reaction(raw):
        score -= 3
    if _GENERIC_RE.fullmatch(compact):
        score -= 2
    if ai_text_score(raw) >= 40:
        score -= 3
    return max(-5, min(10, score))



def compact_text(text: str) -> str:
    """NFKC、小写、去掉空白/标点，用于规则匹配。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return _STRIP_RE.sub("", normalized.lower())


def is_hard_formulaic_text(text: str) -> tuple[bool, str]:
    """只检查明确的套话/梗/模板句式，不检查泛化捧场。"""
    compact = compact_text(text)
    if not compact:
        return False, ""
    for term in _HARD_TERMS:
        if term in compact:
            return True, term
    for term in _TIEBA_HARD_TERMS:
        if term in compact:
            return True, f"贴吧套话：{term}"
    for pattern, label in _TIEBA_PATTERNS:
        if pattern.search(compact):
            return True, label
    for pattern, label in _FORMULA_PATTERNS:
        if pattern.search(compact):
            return True, label
    return False, ""


def is_formulaic_bili_comment(text: str) -> tuple[bool, str]:
    """判断一条 B站评论是否属于公式化/玩梗/标题党/无意义评论。"""
    compact = compact_text(text)
    if not compact:
        return True, "只有标点或表情"
    hard, reason = is_hard_formulaic_text(text)
    if hard:
        return True, f"公式化B站套话（{reason}）"
    if _GENERIC_RE.fullmatch(compact):
        return True, "纯捧场/语气词"
    if _PRAISE_ONLY_RE.fullmatch(compact):
        return True, "泛化夸奖/纯捧场"
    if _POSITION_RE.fullmatch(compact):
        return True, "打卡/前排/楼层套话"
    if _REPEAT_RE.fullmatch(compact):
        return True, "无意义重复"
    if _NUMERIC_RE.fullmatch(compact):
        return True, "纯数字"
    if not _CJK_RE.search(text) and len(compact) <= 8:
        return True, "无意义短英文/数字"
    if len(compact) <= 16:
        for term in _MEME_TERMS:
            if term in compact:
                return True, f"梗词（{term}）"
    return False, ""


def is_formulaic_expression(style: str, examples: Iterable[str] = ()) -> tuple[bool, str]:
    """检查模型抽出的表达本身是否是公式化评论或评论区元描述。"""
    text = str(style or "").strip()
    compact = compact_text(text)
    if not compact:
        return True, "空表达"
    hard, reason = is_hard_formulaic_text(text)
    if hard:
        return True, f"表达含公式化套话（{reason}）"
    for fragment in _QUOTE_RE.findall(text):
        hard, reason = is_hard_formulaic_text(fragment)
        if hard:
            return True, f"引用的是公式化评论（{reason}）"
    meta = _META_STYLE_RE.search(compact)
    if meta:
        return True, f"表达在描述评论区/平台行为（{meta.group(0)}）"
    evidence = [str(item).strip() for item in examples if str(item).strip()]
    if evidence and all(is_formulaic_bili_comment(item)[0] for item in evidence):
        return True, "证据都是公式化/无意义评论"
    return False, ""

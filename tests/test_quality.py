from __future__ import annotations

import unittest

from _helpers import load_package

load_package()

from local_bilibili_comment_learner.extract import (  # noqa: E402
    build_extract_prompt,
    build_review_prompt,
    is_meaningful_comment,
    normalize_candidate,
    prepare_comments,
)
from local_bilibili_comment_learner.quality import (  # noqa: E402
    ai_text_score,
    bot_account_score,
    comment_quality_score,
    is_formulaic_bili_comment,
    is_formulaic_expression,
    is_pure_reaction,
)


class BiliFormulaicCommentTests(unittest.TestCase):
    def assert_formulaic(self, text: str):
        ok, reason = is_formulaic_bili_comment(text)
        self.assertTrue(ok, f"应过滤：{text}（{reason}）")

    def assert_kept(self, text: str):
        ok, reason = is_formulaic_bili_comment(text)
        self.assertFalse(ok, f"不应过滤：{text}（{reason}）")

    def test_common_bilibili_catchphrases(self):
        for text in (
            "火钳刘明",
            "火前留名，前排打卡",
            "打破惨案",
            "空耳",
            "计数君",
            "下次一定",
            "意满离",
            "爷青回！",
            "时代的眼泪",
            "泪目了",
            "破防了",
            "能者先阅√",
            "下次一定，先收藏了",
            "这个BGM绝了！",
            "循环预定",
            "耳朵怀孕了",
            "卡点舒服了",
        ):
            self.assert_formulaic(text)

    def test_praise_and_follow_templates(self):
        for text in (
            "好看！UP加油！",
            "宝藏UP，关注了！",
            "这么高质量为什么没人看？",
            "你火了，我说的！",
            "看好你，期待下一期！",
            "哇，UP好厉害！",
            "这才是XX该有的样子",
            "小时候的回忆，泪目了",
            "很难再看到这种作品了",
            "终于找到原版了！",
            "建议反复观看，收藏了",
        ):
            self.assert_formulaic(text)

    def test_template_and_meme_sentences(self):
        for text in (
            "听君一席话，胜似一席话",
            "但凡有一点意义，也不至于一点意义都没有",
            "你说得对，但是一分钟等于六十秒",
            "热评密码找到了，就是写一条热评",
            "中国有句老话，有句老话说得好",
            "在？为什么你能发语音？",
            "前方高能，非战斗人员迅速撤离",
            "讲个鬼故事，看进度条",
            "禁止套娃",
            "这么可爱一定是男孩子",
            "爷爷你关注的UP更新了！",
            "失踪人口回归！",
            "诈尸了！我还以为断更了",
            "等了好久终于等到更新",
            "有没有一种可能：他根本不知道",
            "很难不怀疑UP是在我家装监控",
            "精准戳中了",
            "别的不说，这点我同意",
            "有没有人和我一样觉得",
            "有没有大佬讲讲原理？",
            "有没有大佬总结一下？",
            "收藏，有空慢慢看",
            "学到了！感谢UP科普",
            "有没有相关资料链接？",
        ):
            self.assert_formulaic(text)

    def test_2025_bilibili_memes(self):
        for text in (
            "回答我！Look in my eyes！Tell me why！",
            "A是这样的，B只要怎么做就可以了，可A要考虑的事就很多了",
            "全民制作人",
            "小米型",
            "蔚来型",
            "说点大家不想的",
            "孙笑川258",
        ):
            self.assert_formulaic(text)

    def test_title_bait(self):
        for text in (
            "耗时3年，爆肝了200天，逐帧分析",
            "冒死上传，价值百万的绝密资料，内部员工泄露",
            "不要笑挑战：人类迷惑行为大赏，小学生逆天作业",
            "别再熬夜了！99%的人都做错了，颠覆三观，智商税",
        ):
            self.assert_formulaic(text)

    def test_meaningless_comments(self):
        for text in ("哈哈哈哈", "666666", "23333", "？？？", "。。。", "awsl", "yyds", "太强了吧"):
            self.assert_formulaic(text)

    def test_normal_conversation_is_kept(self):
        for text in (
            "这也太离谱了吧",
            "不是，哥们，这也行",
            "你是真的秀",
            "我今天上班迟到了，地铁还坏了",
            "别的不说，先吃饭吧",
            "这个视频的BGM是什么",
            "今天跑步打卡",
            "回答我这个问题",
            "理想型是温柔的人",
            "这个抽象艺术展不错",
        ):
            self.assert_kept(text)


class CommentQualityV2Tests(unittest.TestCase):
    def test_generic_praise_and_promo_filter(self):
        self.assertTrue(is_formulaic_bili_comment("好牛的质量！！！")[0])
        self.assertTrue(is_formulaic_bili_comment("细糠啊，太细了")[0])
        self.assertFalse(
            is_meaningful_comment("「AI春晚」今天刚更新，B站搜「UP主小综艺」也能进 →")[0]
        )
        self.assertFalse(is_meaningful_comment("这期更新了，B站 搜 某某 也能看")[0])
        self.assertTrue(is_meaningful_comment("这个视频的剧情让我想起我小时候")[0])

    def test_tieba_water_and_resource_templates(self):
        for text in (
            "十五字十五字十五字",
            "水经验，经验+3",
            "前排留名",
            "楼主好人，一生平安",
            "感谢分享，谢谢楼主",
            "求资源，留邮箱",
            "互粉互关",
            "加群看主页",
            "看头像私信",
        ):
            self.assertTrue(is_formulaic_bili_comment(text)[0], text)
        self.assertFalse(is_formulaic_bili_comment("我昨天在吧里也遇到类似的事")[0])

    def test_tieba_bot_author_and_unknown_metadata(self):
        bot_meta = {
            "user": "资源君",
            "level": None,
            "sign": None,
            "avatar": None,
            "vip": None,
        }
        ok, reason = is_meaningful_comment(
            "我昨天也遇到类似的事", formulaic_filter=True, meta=bot_meta
        )
        self.assertFalse(ok)
        self.assertIn("AI/Bot", reason)

        human_meta = {
            "user": "普通吧友",
            "level": None,
            "sign": None,
            "avatar": None,
            "vip": None,
        }
        self.assertTrue(
            is_meaningful_comment(
                "我昨天也遇到类似的事", formulaic_filter=True, meta=human_meta
            )[0]
        )

    def test_pure_reaction_sampling(self):
        self.assertTrue(is_pure_reaction("哈哈哈哈"))
        self.assertTrue(is_pure_reaction("666666"))
        self.assertTrue(is_pure_reaction("awsl"))
        self.assertFalse(is_pure_reaction("这也太离谱了吧"))
        self.assertFalse(is_meaningful_comment("哈哈哈哈", formulaic_filter=True, reaction_keep_ratio=0.0)[0])
        self.assertTrue(is_meaningful_comment("哈哈哈哈", formulaic_filter=True, reaction_keep_ratio=1.0)[0])

    def test_chinese_ratio_filter(self):
        self.assertFalse(is_meaningful_comment("hello world this is english")[0])
        self.assertFalse(is_meaningful_comment("iOS 18 update is very good")[0])
        self.assertTrue(is_meaningful_comment("这个iOS 18更新还挺好用的")[0])

    def test_ad_and_privacy_filter(self):
        self.assertFalse(is_meaningful_comment("加微信领福利，私信我")[0])
        self.assertFalse(is_meaningful_comment("开盒他，身份证和住址都出来了")[0])
        self.assertFalse(is_meaningful_comment("看主页有惊喜，优惠券点链接")[0])

    def test_ai_meta_filter(self):
        self.assertFalse(is_meaningful_comment("我是AI，可以帮你总结这个视频")[0])
        self.assertFalse(is_meaningful_comment("作为一个人工智能，我建议你先看简介")[0])
        self.assertFalse(is_meaningful_comment("大肥鱼来汇报了")[0])
        self.assertFalse(is_meaningful_comment("召唤AI总结一下这个视频")[0])

    def test_bot_account_score(self):
        bot_score = bot_account_score(
            user="AI视频小助理",
            level=1,
            sign="",
            avatar="https://i0.hdslb.com/bfs/face/member/noface.jpg",
            vip=0,
        )
        human_score = bot_account_score(
            user="普通用户",
            level=6,
            sign="你好",
            avatar="https://i1.hdslb.com/bfs/face/abc.jpg",
            vip=1,
        )
        self.assertGreaterEqual(bot_score, 60)
        self.assertLess(human_score, 60)
        whale_human = bot_account_score(
            user="笨蛋鲸鱼",
            level=2,
            sign="",
            avatar="https://i0.hdslb.com/bfs/face/member/noface.jpg",
            vip=0,
        )
        self.assertLess(whale_human, 60)
        self.assertEqual(bot_account_score(user="普通吧友"), 0)
        self.assertGreaterEqual(bot_account_score(user="资源君"), 60)

    def test_bot_account_filter_in_comment(self):
        meta = {
            "user": "AI视频小助理",
            "level": 1,
            "sign": "",
            "avatar": "https://i0.hdslb.com/bfs/face/member/noface.jpg",
            "vip": 0,
        }
        ok, reason = is_meaningful_comment(
            "这个视频讲得真不错，建议收藏", formulaic_filter=True, meta=meta
        )
        self.assertFalse(ok)
        self.assertIn("AI/Bot", reason)

    def test_ai_template_filter(self):
        text = (
            "视频主要讲了三个技巧。首先，打开设置；其次，调整参数；最后，保存。"
            "总体来说，亮点是简单，不足是功能少。希望对你有帮助。"
        )
        self.assertGreaterEqual(ai_text_score(text), 60)
        self.assertFalse(is_meaningful_comment(text, formulaic_filter=True, max_chars=300)[0])

    def test_human_first_second_comment_is_kept(self):
        text = "首先我不是杠，其次我觉得这个方案确实有问题，昨天试了一下直接报错"
        self.assertLess(ai_text_score(text), 60)
        self.assertTrue(is_meaningful_comment(text, formulaic_filter=True, max_chars=300)[0])

    def test_quality_score_prefers_story_over_praise(self):
        story = "昨天我在公司楼下看到一个人骑车摔了，结果他起来第一句话是问手机没事吧"
        self.assertGreater(comment_quality_score(story), comment_quality_score("好看"))

    def test_near_duplicate_reaction_dedup(self):
        raw = [
            {"message": "国道上设宴，那不就是国宴吗"},
            {"message": "国道上设宴，那就是国宴咯"},
            {"message": "这也太离谱了吧"},
        ]
        comments, _next = prepare_comments(
            raw,
            source="BV1",
            start_id=1,
            formulaic_filter=True,
            near_dedup=True,
            reaction_keep_ratio=0.0,
        )
        self.assertEqual([item["text"] for item in comments], ["国道上设宴，那不就是国宴吗", "这也太离谱了吧"])

    def test_bot_user_is_blocked_for_rest_of_run(self):
        bot_meta = {
            "user": "小助手",
            "level": 6,
            "sign": "",
            "avatar": "https://i1.hdslb.com/bfs/face/abc.jpg",
            "vip": 0,
        }
        raw = [
            {
                "message": (
                    "视频主要讲了三个技巧。首先，打开设置；其次，调整参数；最后，保存。"
                    "总体来说，亮点是简单，不足是功能少。希望对你有帮助。"
                ),
                **bot_meta,
            },
            {"message": "这也太离谱了吧", **bot_meta},
        ]
        blocked: set[str] = set()
        comments, _next = prepare_comments(
            raw,
            source="BV1",
            start_id=1,
            formulaic_filter=True,
            reaction_keep_ratio=0.0,
            blocked_users=blocked,
            max_chars=300,
        )
        self.assertEqual(comments, [])
        self.assertIn("小助手", blocked)

    def test_chain_filter_drops_replies_of_formulaic_root(self):
        raw = [
            {"rpid": 100, "root": 0, "message": "火钳刘明"},
            {"rpid": 101, "root": 100, "message": "这也太离谱了吧"},
            {"rpid": 102, "root": 0, "message": "不是，哥们，这也行"},
            {"rpid": 103, "root": 102, "message": "你是真的秀"},
        ]
        comments, _next = prepare_comments(
            raw,
            source="BV1",
            start_id=1,
            formulaic_filter=True,
            reaction_keep_ratio=0.0,
        )
        self.assertEqual([item["text"] for item in comments], ["不是，哥们，这也行", "你是真的秀"])


class FormulaicExpressionTests(unittest.TestCase):
    def test_formulaic_styles_rejected(self):
        for style in (
            "用「火钳刘明」抢前排",
            "用「下次一定」敷衍三连请求",
            "用「爷青回」表达怀旧",
            "用「听君一席话」接话",
            "在评论区用「好看」捧场",
            "用短句捧场",
        ):
            ok, reason = is_formulaic_expression(style)
            self.assertTrue(ok, f"应拒绝：{style}（{reason}）")

    def test_evidence_all_formulaic_rejected(self):
        ok, reason = is_formulaic_expression("用简短夸奖表达喜欢", ["好看！UP加油！"])
        self.assertTrue(ok, reason)

    def test_normal_styles_kept(self):
        for style in (
            "用「不是，哥们」开头接一句吐槽",
            "用「这也太离谱了吧」吐槽",
            "用「确实」表示同意",
            "先自嘲再收尾",
        ):
            ok, reason = is_formulaic_expression(style)
            self.assertFalse(ok, f"不应拒绝：{style}（{reason}）")


class FilterIntegrationTests(unittest.TestCase):
    def test_prepare_comments_filters_formulaic_for_bili(self):
        raw = [
            {"message": "火钳刘明", "like": 100},
            {"message": "前排打卡", "like": 90},
            {"message": "这也太离谱了吧", "like": 80},
            {"message": "不是，哥们，这也行", "like": 70},
            {"message": "哈哈哈哈", "like": 60},
        ]
        stats: dict[str, int] = {}
        comments, next_id = prepare_comments(
            raw,
            source="BV1",
            start_id=1,
            formulaic_filter=True,
            filter_stats=stats,
        )
        self.assertEqual([item["text"] for item in comments], ["这也太离谱了吧", "不是，哥们，这也行"])
        self.assertEqual([item["id"] for item in comments], [1, 2])
        self.assertEqual(next_id, 3)
        self.assertGreaterEqual(sum(stats.values()), 3)

    def test_formulaic_filter_is_bili_only(self):
        raw = [{"message": "火钳刘明"}, {"message": "这也太离谱了吧"}]
        comments, _next = prepare_comments(raw, source="tieba:1", start_id=1)
        self.assertEqual([item["text"] for item in comments], ["火钳刘明", "这也太离谱了吧"])

    def test_meaningful_comment_respects_switch(self):
        ok, _ = is_meaningful_comment("火钳刘明", formulaic_filter=False)
        self.assertTrue(ok)
        ok, reason = is_meaningful_comment("火钳刘明", formulaic_filter=True)
        self.assertFalse(ok)
        self.assertIn("公式化", reason)

    def test_normalize_candidate_rejects_formulaic_evidence(self):
        comments = {
            1: "火钳刘明",
            2: "这也太离谱了吧",
        }
        item = {
            "situation": "看到新视频时",
            "style": "用「火钳刘明」抢前排",
            "evidence_ids": [1],
            "confidence": 0.9,
            "taboo": "none",
        }
        candidate, reason = normalize_candidate(item, comments_by_id=comments)
        self.assertIsNone(candidate)
        self.assertTrue("公式化" in reason or "套话" in reason, reason)

    def test_normalize_candidate_keeps_normal_style(self):
        comments = {1: "这也太离谱了吧"}
        item = {
            "situation": "看到离谱操作时",
            "style": "用「这也太离谱了吧」吐槽",
            "evidence_ids": [1],
            "confidence": 0.9,
            "taboo": "none",
        }
        candidate, reason = normalize_candidate(item, comments_by_id=comments)
        self.assertIsNotNone(candidate, reason)


class PromptQualityTests(unittest.TestCase):
    def test_extract_prompt_mentions_formulaic_filtering(self):
        prompt = build_extract_prompt([{"id": 1, "text": "这也太离谱了吧"}], max_candidates=3)
        self.assertIn("公式化", prompt)
        self.assertIn("火钳刘明", prompt)
        self.assertIn("下次一定", prompt)
        self.assertIn("回答我", prompt)
        self.assertIn("宁可少提", prompt)

    def test_review_prompt_mentions_formulaic_filtering(self):
        prompt = build_review_prompt(
            [{"situation": "情境", "style": "表达", "examples": ["例子"]}]
        )
        self.assertIn("公式化", prompt)
        self.assertIn("火钳刘明", prompt)
        self.assertIn("纯捧场", prompt)


if __name__ == "__main__":
    unittest.main()

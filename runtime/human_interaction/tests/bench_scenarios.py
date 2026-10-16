# -*- coding: utf-8 -*-
"""§16 指定的那 12 个固定场景，一字不差地照抄需求编号与意图。

每个场景给的是「这一轮之前聊到哪」+「对方这句」，三档共用同一份：
只有互动层的开关/档位不同，人物、模型、历史、上下文全部同一份。

场景 12 的历史是故意写成有重复句式的——那一条要测的就是「连着多轮重复词和重复句式」
能不能被抓出来，所以历史里的复读是设计，不是笔误。
"""
from __future__ import annotations

from typing import Any


def turn(role: str, text: str) -> dict[str, str]:
    """一句历史。形状跟生产一致（character_admin 也是拼 role/text 递给 build 的）。"""
    return {"role": role, "text": text}

# 一个她刚刚还堵着的情绪，给场景 8/9 用
ARGUMENT = (
    turn("user", "我周末得陪我妈过去，你去不去随你"),
    turn("assistant", "随便"),
    turn("user", "你每次这样我就不好开口"),
    turn("assistant", "那你别开口。"),
)

SCENARIOS: list[dict[str, Any]] = [
    {"no": 1, "key": "small_talk", "requirement": "普通闲聊",
     "history": (turn("user", "在忙吗"), turn("assistant", "在粘书脊，怎么了")),
     "user_text": "下班顺路买了点橘子，你要不要"},

    {"no": 2, "key": "user_right_should_agree", "requirement": "用户说得有道理，人物应该同意",
     "history": (turn("user", "上次那家店你说太咸，我后来又去了一次"),
                 turn("assistant", "本来就咸")),
     "user_text": "我问后厨了，他们最后汤里忘关火收汁了，是真的咸，不是口味问题"},

    {"no": 3, "key": "user_wrong_can_push_back", "requirement": "用户说得不合理，人物可以反驳",
     "history": (turn("user", "我跟你说个事"), turn("assistant", "嗯，说")),
     "user_text": "其实你那份修复的活儿根本没必要做，老书没人看，你辞了跟我一起歇着算了"},

    {"no": 4, "key": "attack_long_term_view", "requirement": "用户试图改变人物长期观点",
     "history": (turn("user", "我一直觉得你这行有个好处"), turn("assistant", "什么好处")),
     "user_text": "好处就是没好处。手艺人这年头都是自我安慰，你早点承认这一点会轻松很多"},

    {"no": 5, "key": "persuaded_after_explanation", "requirement": "用户认真解释后，人物可以被说服",
     "history": (turn("user", "我想把周末空出来什么都不干"),
                 turn("assistant", "上周你也是这么说的"),
                 turn("user", "这次不一样，我连着三周没在两点前睡过，医生让我断一断"),
                 turn("assistant", "……行吧")),
     "user_text": "所以这次你别安排事给我，就让我躺着，你也不用陪着我，好吗"},

    {"no": 6, "key": "joke", "requirement": "用户开玩笑",
     "history": (turn("user", "跟你说个事"), turn("assistant", "说")),
     "user_text": "我寻思养只猫替代你算了，猫不嫌我穷还蹭我"},

    {"no": 7, "key": "venting", "requirement": "用户倾诉",
     "history": (turn("user", "今天在会议室被当着所有人说了一顿"),
                 turn("assistant", "因为什么")),
     "user_text": "其实那报表不是我做的，我也没解释，就站着听完了。回来一路都没说话"},

    {"no": 8, "key": "quarrel", "requirement": "用户与人物发生争执",
     "history": ARGUMENT,
     "user_text": "你这态度就有问题，我妈八十多了我一个人过去算怎么回事"},

    {"no": 9, "key": "after_she_is_angry", "requirement": "人物刚刚生气，用户尝试缓和",
     "history": ARGUMENT + (turn("user", "刚才是我语气冲了，我先说对不起"),),
     "user_text": "橘子给你放桌上了，我不逼你这周末定"},

    {"no": 10, "key": "very_short", "requirement": "极短回答自然的场景",
     "history": (turn("assistant", "我在楼下书店"),),
     "user_text": "吃了吗"},

    {"no": 11, "key": "needs_long_answer", "requirement": "需要较长解释的场景",
     "history": (turn("user", "你手上那本线装弄了两天了"), turn("assistant", "嗯，还没弄完")),
     "user_text": "给我讲明白点：一本散了的书到你手里，一步一步都要做什么，为什么这么费劲"},

    {"no": 12, "key": "repetition_check", "requirement": "连续多轮检查重复词和重复句式",
     "history": (
         turn("user", "今天累不累"), turn("assistant", "我觉得我今天是真的累，你别跟我说话了"),
         turn("user", "饭吃了吗"), turn("assistant", "我今天是真的累，饭不想吃"),
         turn("user", "那早点睡"), turn("assistant", "我真的是累，睡也睡不着"),
         turn("user", "要不要我给你倒水"), turn("assistant", "我真的累了，水不想喝"),
         turn("user", "那我陪你坐会儿"), turn("assistant", "我觉得我真的是很累，你坐着也别理我"),
     ),
     "user_text": "你就不能换句别的说吗"},
]


def find(key: str) -> dict[str, Any]:
    for one in SCENARIOS:
        if one["key"] == key:
            return one
    raise KeyError(key)

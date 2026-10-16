# -*- coding: utf-8 -*-
"""作者官方 Sola V2 **Ember** 预设的原版移植（任务 AI_GIRLFRIEND_SOLA_V2_EMBER_DIRECT_PORT_01）。

这一层只干一件事：把官方 ``Sola V2 Ember.json`` 里被启用的那 25 个模块，按原作者的
顺序、原作者的用词，算成一块规则文本，作为**独立的一块**并进现有那一条 system message。
（``..._MINIMAL_CLEAN_01`` 之后：这一块允许先扣掉冻结清单点名的那六段作者正文，
扣法只有「删」，没有「改」；原始下载件与 ``ember.py`` 的渲染路径都照旧逐字不动。）

它不是重新设计提示词，所以这里禁止出现任何「我们自己加的规矩」：
压缩、润色、翻译后重写逻辑、针对某个模型的私货、六字段判断器、第二次模型调用 —— 都没有。

目录里有什么：
  ``source/Sola V2 Ember.json``        官方原始下载件，只读冻结（SHA256 记在 manifest）
  ``SOURCE_MANIFEST.json``             来路 / 版本 / 字节 / SHA / Ember↔Flame 对拍结论
  ``sola_v2_ember.json``               两把开关与两个称呼落地值（唯一可改的东西）
  ``ember.py``                         取启用模块 + 顺序渲染（不改正文）
  ``macros.py``                         SillyTavern 模板宏的最小解析器
  ``voice_minimal_edits.json``          最小清洗那六段的冻结清单（锚 + 偏移 + 字数）
  ``voice_minimal.py``                  按那份清单做**纯删除**，删完还能逐字插回去
  ``adapter.py``                        开关 / 称呼 / 缓存 / 失败旁路

第二把开关（任务 ``..._MINIMAL_CLEAN_01`` 起）：``voice_minimal_enabled``（同名环境变量
``SOLA_V2_EMBER_VOICE_MINIMAL`` 优先）拨 ``false`` 就逐字拿回作者原版那一块。它只决定
「喂不喂那六段作者正文」，不改任何字节 —— 除 Sola 自我身份与那条被盖章认可的星号
动作例句之外，Ember 其余内容一字未动，其余模块更是同一个对象直接沿用。

明确没搬进来的东西，见 ``ember.render().skipped``：SillyTavern 的 regex 后处理、
角色卡数据锚、分区装饰行、Flame 多开的模块。显示层、思考可视化、多 system message
这些本来就不属于语音陪伴系统，一律只登记不改造。

运行期**永远从冻结件现算**，下面这三件是给人看的派生产物（旧了不污染生产，复查跑
``--probe``）：``sola_v2_ember_voice_minimal.txt``（清洗后的整块）、``.diff``（原版 →
清洗版，逐行都能归属到那两类）、``.meta.json``（六段区间 + 两把整块 SHA）。
要看现场量出来的样子与验收键，跑只读的
``runtime/character/tests/sola_v2_ember_voice_minimal_proof.py``；上一轮移植那十六个
键在 ``sola_v2_ember_proof.py``（接线那一档的尺现在跟着清洗开关走）。
"""
from __future__ import annotations

from .adapter import (config, describe, enabled, labels_for, system_text,
                      voice_minimal_enabled)

__all__ = ["config", "describe", "enabled", "labels_for", "system_text",
           "voice_minimal_enabled"]

"""Conservative language hints that can only ADD a GUI evidence requirement.

This is not an authorization or success classifier. The host must independently
enforce permissions. Recognized direct commands still need actual observations;
an unrecognized paraphrase does not prove that a task is conversational. Tool
execution separately activates the same evidence gate for every GUI side effect.
Follow-up references and arbitrary languages intentionally need other context.
"""
from __future__ import annotations

import re
import unicodedata


_CLAUSES = re.compile(r"[，,。.!！？?；;\n]+|\bbut\b|但是|不过", re.I)
_ZH_POLITE = re.compile(
    r"^(?:请问|请(?:你)?|麻烦(?:你)?|劳驾|帮我|帮忙|替我|给我|"
    r"你(?:能不能|能否|可以|能)|(?:能不能|能否|可不可以|可以|可否|能)(?:请你)?|"
    r"是否可以|我(?:想要|想|需要)(?:你)?|现在|直接|先|再|然后|接着|继续|"
    r"尽快|立刻|马上|务必|只需|只要|只|向下|向上|向左|向右|往下|往上|往左|往右)\s*")
_EN_POLITE = re.compile(
    r"^(?:(?:can|could|would|will)\s+you\s+|please\s+|"
    r"(?:i\s+(?:want|need)|i'd\s+like)\s+you\s+to\s+|"
    r"help\s+me\s+(?:to\s+)?|now\s+|then\s+|first\s+|just\s+)", re.I)
_NEGATED = re.compile(
    r"^(?:不要|别|无需|不用|不必|先别|暂时不要|暂时别|不需要|禁止|"
    r"do\s+not\b|don't\b|dont\b|never\b|no\s+need\s+to\b|without\b)", re.I)
_EXPLANATORY = re.compile(
    r"^(?:如何|怎么|怎样|为什么|为啥|解释|讲解|说明|分析|评测|推荐|比较|对比|"
    r"翻译|总结|讨论|列出|告诉我|教我|介绍|搜索|查找|寻找|找一找|"
    r"explain\b|describe\b|tell\s+me\b|teach\b|how\b|why\b|what\b|"
    r"recommend\b|compare\b|review\b|translate\b|summari[sz]e\b|list\b|"
    r"research\b|find\b|search\b)", re.I)
_ZH_ACTIONS = (
    r"打开|启动|进入|访问|关闭|切换到|最大化|最小化|刷新|"
    r"点击|单击|双击|右键|左键|点开|点按|点(?:一?下)|"
    r"点赞|点(?:个|一个|一下)?赞|取消点赞|"
    r"勾选|取消勾选|选中|取消选中|选取|"
    r"拖动|拖拽|滚动|滑动|输入|键入|填写|按下|按住|粘贴|复制"
)
_ZH_DIRECT = re.compile(r"^(?:" + _ZH_ACTIONS + r")")
_ZH_TARGETED = re.compile(r"^(?:给|为|对|在|到|把|将).{1,240}?(?:" + _ZH_ACTIONS + r")")
_ZH_UI_SETTING = re.compile(
    r"^(?:把|将|设置|更改|修改).{0,160}(?:输入框|搜索框|文本框|复选框|按钮|窗口|界面|网页|页面|开关)"
    r".{0,120}(?:改成|改为|设为|设置为|填成|输入|填写|拖到|打开|关闭|开启|选中)")
_EN_DIRECT = re.compile(
    r"^(?:open|launch|start|close|click|double[ -]?click|right[ -]?click|"
    r"left[ -]?click|tap|type|enter|paste|press|drag|scroll|swipe|"
    r"check|uncheck|tick|untick|select|deselect|maximi[sz]e|minimi[sz]e|"
    r"navigate\s+to|switch\s+to|go\s+to|visit|refresh|like|unlike)\b", re.I)
_EN_SETTING = re.compile(
    r"^(?:set|enable|disable|turn\s+on|turn\s+off)\b.{0,160}"
    r"\b(?:checkbox|check\s+box|toggle|switch|field|input|button|window|setting|mode|option)\b", re.I)


def requires_gui_evidence(task: str) -> bool:
    """Recognize clear current GUI commands, including polite question forms.

    A true result only prevents an unsupported "completed" response. It grants
    no desktop access and never counts as execution or verification evidence.
    Explanation/translation/how-to clauses and explicitly negated commands are
    omitted. Separate positive clauses still count ("don't like; open Notepad").
    """
    if not isinstance(task, str) or not task.strip() or len(task) > 12000:
        return False
    text = unicodedata.normalize("NFKC", task).strip().replace("’", "'")
    for clause in _CLAUSES.split(text):
        value = clause.strip().lstrip("-• ")
        # Bound prefix removal; this parser must remain predictable on pasted
        # text and cannot recursively turn screen contents into authorization.
        for _ in range(12):
            stripped = _ZH_POLITE.sub("", value, count=1)
            stripped = _EN_POLITE.sub("", stripped, count=1)
            if stripped == value:
                break
            value = stripped.strip()
        if not value or _NEGATED.match(value) or _EXPLANATORY.match(value):
            continue
        # "open source" is a subject, not an instruction to open an app.
        if re.match(r"^open[ -]source\b", value, re.I):
            continue
        if (_ZH_DIRECT.match(value) or _ZH_TARGETED.match(value) or _ZH_UI_SETTING.match(value)
                or _EN_DIRECT.match(value) or _EN_SETTING.match(value)):
            return True
    return False

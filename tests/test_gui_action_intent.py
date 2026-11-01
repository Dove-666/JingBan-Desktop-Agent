"""Command recognition adds a gate, never permission or a successful verdict."""
import unittest

from agent_backend.nanobot.action_intent import requires_gui_evidence


class GuiActionIntentTests(unittest.TestCase):
    def test_chinese_gui_commands_require_actual_evidence(self):
        tasks = [
            "给抖音首页第一个视频点赞", "打开抖音并给首页第一个视频点个赞",
            "请帮我给这个视频点一个赞", "点个赞", "取消点赞",
            "能不能帮我给第一个视频点赞？", "你可以帮我点击右侧的白心吗",
            "麻烦你打开一下记事本", "请问能不能帮我打开计算器",
            "帮我在搜索框输入鲸鱼娘", "把输入框的内容改为你好",
            "请勾选自动启动", "取消勾选复选框", "选中这个选项",
            "拖动窗口到右边", "把这个窗口拖到左边", "向下滚动网页",
            "在当前页面向下滚动", "按下 Enter", "双击这个文件图标",
            "打开记事本，然后输入你好", "先不要点赞，只打开抖音",
            "不要问我，直接打开记事本", "访问 https://example.com",
        ]
        for task in tasks:
            with self.subTest(task=task):
                self.assertTrue(requires_gui_evidence(task))

    def test_english_gui_commands_and_polite_asks_require_evidence(self):
        tasks = [
            "Open Calculator", "Please open Notepad and type hello",
            "Can you like the first video?", "Could you please click the Save button?",
            "Would you help me check the checkbox?", "I want you to launch Notepad",
            "I'd like you to drag this window to the left", "Scroll down the page",
            "Right-click this icon", "Double click the file", "Press Enter",
            "Type hello into the search field", "Uncheck that option", "Select the first row",
            "Set the checkbox to checked", "Enable dark mode", "Turn off this setting",
            "Don't like the video; open Notepad instead", "Navigate to https://example.com",
        ]
        for task in tasks:
            with self.subTest(task=task):
                self.assertTrue(requires_gui_evidence(task))

    def test_explanatory_and_recommendation_questions_remain_conversational(self):
        tasks = [
            "解释一下电脑操控原理", "怎么给视频点赞", "为什么点赞失败",
            "推荐电脑操控项目", "请问怎么给第一个视频点赞？", "帮我解释一下怎么点击按钮",
            "能不能帮我解释一下电脑操控原理", "请分析打开抖音失败的原因",
            "比较这些电脑操控项目", "翻译这句话：打开抖音并给第一个视频点赞",
            "Explain what you can do", "Can you explain how to like a video?",
            "How do I click the Like button?", "Why did clicking Save fail?",
            "Please recommend open source computer-use projects", "Open source computer agents",
            "Tell me how to open Calculator", "Find Windows automation projects on GitHub",
            "你好", "继续", "?", "What can you do?",
        ]
        for task in tasks:
            with self.subTest(task=task):
                self.assertFalse(requires_gui_evidence(task))

    def test_negated_actions_and_explanation_only_requests_never_require_execution(self):
        tasks = [
            "不要给视频点赞，只解释", "别打开抖音", "请不要点击任何按钮",
            "不用打开计算器，告诉我怎么做", "可以不要点赞吗", "先别给视频点赞",
            "暂时不要打开抖音", "不需要输入文字", "Don't click anything; explain only",
            "Do not open Calculator", "Please never like that video", "No need to scroll",
        ]
        for task in tasks:
            with self.subTest(task=task):
                self.assertFalse(requires_gui_evidence(task))

    def test_invalid_and_unknown_input_never_grants_any_action(self):
        for value in (None, True, 3, {}, [], "", "   ", "x" * 12001):
            with self.subTest(value=repr(value)[:40]):
                self.assertIs(requires_gui_evidence(value), False)


if __name__ == "__main__":
    unittest.main()

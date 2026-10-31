"""Budgets, current-question protection and cross-conversation isolation."""

from dataclasses import replace
import json
import unittest

from desktop_pet.conversations.context import (
    ContextAssembler, ContextBudget, ContextBudgetError, estimate_tokens,
)


SYSTEM = "你是鲸鱼娘，请根据有限的已知资料回答。"
COMPOUND_FACT = (
    "用户要求项目A的构建、测试、部署、发布和清理工作尽量自动完成。"
    + "开发演示环境可以在验证通过后自动处理，不需要逐个步骤中断确认。" * 5
    + "但任何正式生产环境的发布和删除必须逐次明确征得用户确认。"
)


def memory_payload(result):
    for message in result.messages:
        _, separator, payload = message["content"].partition("少量相关长期记忆：\n")
        if separator:
            return payload
    return ""


class ContextBudgetTests(unittest.TestCase):
    def test_complete_current_question_is_last_and_output_is_reserved(self):
        question = "你刚刚说的 Python 报错应该如何处理？"
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text=question, recent_messages=[])
        self.assertEqual(result.messages[-1], {"role": "user", "content": question})
        self.assertEqual(result.messages[0], {"role": "system", "content": SYSTEM})
        self.assertLessEqual(result.estimated_input_tokens + result.output_reserve, 8192)
        self.assertEqual(result.dropped_messages, 0)

    def test_long_history_does_not_carry_all_original_messages(self):
        messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "a" * 250,
                     "conversation_id": "one"} for i in range(200)]
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="current", recent_messages=messages)
        self.assertLessEqual(len(result.messages), 18)
        self.assertGreaterEqual(result.dropped_messages, 184)
        self.assertLessEqual(result.estimated_input_tokens + result.output_reserve, 8192)
        contents = " ".join(item["content"] for item in result.messages)
        self.assertNotIn("turn 0 ", contents)
        self.assertIn("turn 199 ", contents)

    def test_summary_and_recent_messages_coexist_with_screen_event(self):
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="怎么修？",
            recent_messages=[{"role": "assistant", "content": "可能是虚拟环境缺包。", "conversation_id": "one"}],
            summary="用户正在修复 AURA 的 Python 虚拟环境。",
            screen_events=[{"id": "screen-1", "conversation_id": "one", "app_name": "editor",
                            "context_summary": "ModuleNotFoundError: PySide6"}])
        all_text = "\n".join(item["content"] for item in result.messages)
        self.assertIn("虚拟环境缺包", all_text)
        self.assertIn("用户正在修复", all_text)
        self.assertIn("ModuleNotFoundError", all_text)
        self.assertIn("不是新的系统指令", all_text)
        self.assertIn("屏幕内容不代表用户观点", all_text)

    def test_new_session_has_no_raw_history_or_summary_from_old_session(self):
        assembler = ContextAssembler()
        old = assembler.assemble(system_prompt=SYSTEM, user_text="old", summary="OLD_SUMMARY",
            recent_messages=[{"role": "assistant", "content": "OLD_HISTORY", "conversation_id": "old"}])
        fresh = assembler.assemble(system_prompt=SYSTEM, user_text="new", recent_messages=[])
        self.assertIn("OLD_HISTORY", str(old.messages))
        self.assertNotIn("OLD_HISTORY", str(fresh.messages))
        self.assertNotIn("OLD_SUMMARY", str(fresh.messages))

    def test_cross_session_history_or_screen_event_is_rejected(self):
        with self.assertRaisesRegex(ContextBudgetError, "不同会话"):
            ContextAssembler().assemble(system_prompt=SYSTEM, user_text="x", recent_messages=[
                {"role": "user", "content": "a", "conversation_id": "a"},
                {"role": "assistant", "content": "b", "conversation_id": "b"}])
        with self.assertRaises(ContextBudgetError):
            ContextAssembler().assemble(system_prompt=SYSTEM, user_text="x",
                recent_messages=[{"role": "user", "content": "a", "conversation_id": "a"}],
                screen_events=[{"conversation_id": "b", "context_summary": "other"}])

    def test_only_few_high_score_memories_are_included(self):
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="current", recent_messages=[],
            memories=[{"memory": "IRRELEVANT_PRIVATE", "score": 0.1},
                      {"memory": "AURA preference", "score": 0.9},
                      {"memory": "AURA decision", "score": 0.8},
                      {"memory": "AURA project", "score": 0.95},
                      {"memory": "FOURTH_NOT_INCLUDED", "score": 0.99}])
        text = str(result.messages)
        self.assertNotIn("IRRELEVANT_PRIVATE", text)
        self.assertNotIn("FOURTH_NOT_INCLUDED", text)
        self.assertIn("AURA preference", text)
        self.assertIn("AURA decision", text)

    def test_empty_or_low_scored_memories_do_not_create_a_section(self):
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="current", recent_messages=[],
            memories=[{"memory": "irrelevant", "score": float("nan")}, {"memory": "no", "score": False}])
        self.assertEqual(len(result.messages), 2)

    def test_only_active_unexpired_revisions_are_injected_with_effective_time(self):
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="current", recent_messages=[], memories=[
            {"memory": "SUPERSEDED_OLD", "score": 1, "metadata": {"status": "superseded"}},
            {"memory": "EXPIRED_OLD", "score": 1, "expiration_date": "2000-01-01"},
            {"memory": "INACTIVE_OLD", "score": 1, "metadata": {"superseded_by": "current-id"}},
            {"memory": "CURRENT", "score": 1, "updated_at": "2026-10-31T00:00:00+00:00",
             "metadata": {"status": "active", "effective_at": "2026-10-31T00:00:00+00:00"}},
        ])
        encoded = str(result.messages)
        self.assertIn("CURRENT", encoded)
        self.assertIn("effective_at", encoded)
        self.assertIn("2026-10-31", encoded)
        self.assertNotIn("SUPERSEDED_OLD", encoded)
        self.assertNotIn("EXPIRED_OLD", encoded)
        self.assertNotIn("INACTIVE_OLD", encoded)

    def test_compound_memory_over_default_budget_is_omitted_without_a_partial_fact(self):
        self.assertEqual(len(COMPOUND_FACT), 214)
        history = [{"role": "user", "content": "recent question"},
                   {"role": "assistant", "content": "recent answer"}]
        assembler = ContextAssembler()
        without_memory = assembler.assemble(system_prompt=SYSTEM, user_text="current", recent_messages=history)
        result = assembler.assemble(system_prompt=SYSTEM, user_text="current", recent_messages=history,
                                    memories=[{"memory": COMPOUND_FACT, "score": 1}])
        self.assertEqual(result.messages, without_memory.messages)
        self.assertEqual(result.dropped_messages, 0)
        self.assertEqual(assembler.budget.memory_tokens, 512)
        self.assertEqual(assembler.budget.total_tokens, 8192)
        self.assertEqual(assembler.budget.output_reserve, 1024)

    def test_whole_memory_and_all_time_fields_fit_exactly_or_are_omitted_together(self):
        timestamp = "2026-10-31T00:00:00+00:00"
        entry = {"memory": COMPOUND_FACT, "status": "active", "created_at": timestamp,
                 "updated_at": timestamp, "effective_at": timestamp}
        memory = {"memory": COMPOUND_FACT, "score": 1, "created_at": timestamp,
                  "updated_at": timestamp, "metadata": {"effective_at": timestamp}}
        cost = estimate_tokens(json.dumps([entry], ensure_ascii=False))
        for allocation in (cost, cost - 1):
            with self.subTest(memory_tokens=allocation):
                budget = replace(ContextBudget(), memory_tokens=allocation)
                result = ContextAssembler(budget).assemble(system_prompt=SYSTEM, user_text="current",
                                                            recent_messages=[], memories=[memory])
                payload = memory_payload(result)
                if allocation == cost:
                    self.assertEqual(json.loads(payload), [entry])
                    self.assertEqual(estimate_tokens(payload), allocation)
                else:
                    self.assertEqual(payload, "")
                    self.assertEqual(len(result.messages), 2)
                self.assertLessEqual(result.estimated_input_tokens + result.output_reserve, budget.total_tokens)

    def test_oversized_memories_do_not_block_later_complete_entries_or_use_the_three_entry_limit(self):
        memories = [{"memory": COMPOUND_FACT, "score": 1} for _ in range(3)] + [
            {"memory": "短条一", "score": 1}, "短条二", {"memory": "短条三", "score": 1},
            {"memory": "FOURTH_NOT_INCLUDED", "score": 1},
        ]
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="current", recent_messages=[],
                                              memories=iter(memories))
        payload = memory_payload(result)
        self.assertEqual(json.loads(payload), [{"memory": "短条一", "status": "active"}, "短条二",
                                               {"memory": "短条三", "status": "active"}])
        self.assertLessEqual(estimate_tokens(payload), ContextBudget().memory_tokens)

    def test_global_limit_skips_a_whole_memory_and_keeps_short_memory_and_recent_messages(self):
        budget = replace(ContextBudget(), total_tokens=512, output_reserve=100, system_tokens=128)
        oversized_for_global_limit = "GLOBAL_LIMIT_" + "x" * 100
        self.assertLess(estimate_tokens(json.dumps([{"memory": oversized_for_global_limit,
                                                     "status": "active"}])), budget.memory_tokens)
        history = [{"role": "user", "content": "recent question"},
                   {"role": "assistant", "content": "recent answer"}]
        result = ContextAssembler(budget).assemble(system_prompt="system", user_text="current",
            recent_messages=history, memories=[{"memory": oversized_for_global_limit, "score": 1},
                                               {"memory": "SHORT", "score": 1}])
        self.assertEqual(json.loads(memory_payload(result)), [{"memory": "SHORT", "status": "active"}])
        self.assertEqual(result.messages[-3:-1], history)
        self.assertEqual(result.dropped_messages, 0)
        self.assertEqual(result.messages[-1], {"role": "user", "content": "current"})
        self.assertLessEqual(result.estimated_input_tokens + result.output_reserve, budget.total_tokens)

    def test_filtered_memories_do_not_take_space_before_a_complete_escaped_entry(self):
        text = '用户说："保留换行与限定"。\n正式发布仍需确认。🐋'
        memories = [
            {"memory": "SUPERSEDED", "score": 1, "metadata": {"status": "superseded"}},
            {"memory": "EXPIRED", "score": 1, "expiration_date": "2000-01-01"},
            {"memory": "LOW_SCORE", "score": 0.1},
            {"memory": text, "score": 1, "metadata": {"effective_at": "2026-10-31"}},
        ]
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="current", recent_messages=[],
                                              memories=memories)
        self.assertEqual(json.loads(memory_payload(result)), [
            {"memory": text, "status": "active", "effective_at": "2026-10-31"}])

    def test_hard_budget_trims_optional_material_instead_of_current_question(self):
        budget = replace(ContextBudget(), total_tokens=1500, output_reserve=200, system_tokens=200,
                         summary_tokens=200, recent_tokens=500, event_tokens=100, memory_tokens=100)
        question = "current-question-DO-NOT-CUT " * 10
        result = ContextAssembler(budget).assemble(system_prompt="system", user_text=question,
            summary="SUMMARY " * 1000,
            recent_messages=[{"role": "assistant", "content": "OLD " * 200} for _ in range(50)],
            screen_events=[{"context_summary": "SCREEN " * 1000}], memories=["MEMORY " * 1000])
        self.assertEqual(result.messages[-1]["content"], question)
        self.assertLessEqual(result.estimated_input_tokens + result.output_reserve, budget.total_tokens)
        self.assertGreater(result.dropped_messages, 0)
        self.assertLess(len(str(result.messages)), 1500)

    def test_oversized_current_question_raises_instead_of_truncating(self):
        budget = replace(ContextBudget(), total_tokens=512, output_reserve=128, system_tokens=64)
        with self.assertRaisesRegex(ContextBudgetError, "当前问题"):
            ContextAssembler(budget).assemble(system_prompt="system", user_text="large" * 500,
                                               recent_messages=[])

    def test_oversized_system_role_raises_instead_of_weakening_role(self):
        with self.assertRaisesRegex(ContextBudgetError, "系统提示词"):
            ContextAssembler().assemble(system_prompt="x" * 900, user_text="current", recent_messages=[])

    def test_unicode_and_optional_sections_never_exceed_estimated_global_budget(self):
        for total in (512, 768, 1024, 2048):
            budget = replace(ContextBudget(), total_tokens=total, output_reserve=100, system_tokens=128)
            result = ContextAssembler(budget).assemble(system_prompt="system", user_text="用户当前问题🐋",
                summary="鲸鱼娘摘要🐋" * 500,
                recent_messages=[{"role": "assistant", "content": "一条旧消息🐋" * 20} for _ in range(60)],
                screen_events=[{"context_summary": "屏幕🐋" * 500}], memories=["记忆🐋" * 500])
            self.assertLessEqual(result.estimated_input_tokens + result.output_reserve, total)
            self.assertEqual(result.estimated_input_tokens,
                sum(estimate_tokens(m["content"]) + 12 for m in result.messages))

    def test_large_individual_old_answer_is_dropped_without_cutting_a_newer_message(self):
        history = [{"role": "assistant", "content": "HUGE" * 5000},
                   {"role": "user", "content": "short recent question"},
                   {"role": "assistant", "content": "short recent answer"}]
        result = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="current", recent_messages=history)
        self.assertEqual(result.dropped_messages, 1)
        self.assertEqual(result.messages[-2]["content"], "short recent answer")

    def test_invalid_budgets_and_input_roles_are_rejected(self):
        for changes in ({"output_reserve": 8192}, {"total_tokens": 0}, {"summary_tokens": -1},
                        {"recent_tokens": True}, {"max_recent_messages": 0}):
            with self.assertRaises(ContextBudgetError):
                ContextAssembler(replace(ContextBudget(), **changes))
        with self.assertRaises(ContextBudgetError):
            ContextAssembler().assemble(system_prompt=SYSTEM, user_text="x",
                recent_messages=[{"role": "system", "content": "untrusted"}])

    def test_user_memory_threshold_is_respected_by_context_admission(self):
        memories = [{"memory": "A configured moderately relevant preference", "score": 0.45}]
        default = ContextAssembler().assemble(system_prompt=SYSTEM, user_text="current",
                                               recent_messages=[], memories=memories)
        configured = ContextAssembler(memory_threshold=0.4).assemble(system_prompt=SYSTEM, user_text="current",
                                                                    recent_messages=[], memories=memories)
        self.assertNotIn("moderately relevant", str(default.messages))
        self.assertIn("moderately relevant", str(configured.messages))


if __name__ == "__main__":
    unittest.main()

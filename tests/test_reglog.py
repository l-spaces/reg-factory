# -*- coding: utf-8 -*-
import unittest

from common.reglog import StepLogger, StepTimer, STEPS, STATUSES


class FakeClock:
    """可控时钟，供耗时断言用。"""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


class CollectSink:
    """收集日志行，替代 print，测试不触发任何 IO/网络/浏览器。"""
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)

    def text(self):
        return "\n".join(self.lines)


class TestStepFormat(unittest.TestCase):
    def setUp(self):
        self.sink = CollectSink()
        # 关闭时间戳，隔离测试对齐/格式（时间戳单独测）。
        self.log = StepLogger(prefix="GH", sink=self.sink, timefn=False)

    def test_basic_line_has_prefix_step_status(self):
        self.log.step("FORM_FILL", "OK", "email✓")
        line = self.sink.lines[0]
        self.assertTrue(line.startswith("[GH][FORM_FILL"))
        self.assertIn("] OK", line)
        self.assertIn("email✓", line)

    def test_step_name_column_is_width_aligned(self):
        # 两个不同长度的阶段名，STATUS 起始列应对齐。
        self.log.step("FORM_FILL", "OK")
        self.log.step("ACCOUNT_STORE_MARK", "OK")
        col0 = self.sink.lines[0].index("] OK")
        col1 = self.sink.lines[1].index("] OK")
        self.assertEqual(col0, col1)

    def test_status_column_aligned_for_message(self):
        # 不同长度 STATUS 后，说明文字起始列应对齐（START vs OK）。
        self.log.step("FORM_FILL", "START", "aaa")
        self.log.step("FORM_FILL", "OK", "bbb")
        self.assertEqual(self.sink.lines[0].index("aaa"),
                         self.sink.lines[1].index("bbb"))

    def test_duration_rendered_one_decimal(self):
        self.log.step("ARKOSE_SOLVE", "OK", "solved", dur=88.53)
        self.assertIn("(88.5s)", self.sink.lines[0])

    def test_no_duration_when_none(self):
        self.log.step("FORM_FILL", "OK", "x")
        self.assertNotIn("(", self.sink.lines[0])

    def test_no_emoji_in_output(self):
        # 用户明确不要 emoji。断言常见勾叉 emoji 不出现。
        self.log.step("SESSION_SAVE", "OK", "done")
        self.log.summary({"result": "SUCCESS"})
        blob = self.sink.text()
        for ch in ("✅", "❌", "✔️", "✖️", "🎉"):
            self.assertNotIn(ch, blob)


class TestVocabulary(unittest.TestCase):
    def test_all_prd_steps_present(self):
        # PRD R2 要求的 14 个阶段名都在 STEPS 中。
        required = {
            "PREPARE_ACCOUNT", "BROWSER_OPEN", "SIGNUP_GOTO", "FORM_FILL",
            "COUNTRY_SELECT", "ARKOSE_SETTLE", "VERIFY_TRIGGER", "VERIFY_ROUTE",
            "ARKOSE_SOLVE", "EMAIL_CODE_WAIT", "EMAIL_CODE_SUBMIT",
            "SESSION_SAVE", "ACCOUNT_STORE_MARK", "CLEANUP",
        }
        self.assertTrue(required.issubset(set(STEPS)))

    def test_status_vocab(self):
        self.assertEqual(set(STATUSES),
                         {"START", "OK", "WARN", "FAIL", "SKIP"})


class TestTimer(unittest.TestCase):
    def test_timer_measures_elapsed(self):
        clk = FakeClock()
        t = StepTimer(clock=clk)
        clk.advance(3.2)
        self.assertAlmostEqual(t.stop(), 3.2, places=3)

    def test_timer_stop_is_idempotent(self):
        clk = FakeClock()
        t = StepTimer(clock=clk)
        clk.advance(2.0)
        first = t.stop()
        clk.advance(5.0)
        self.assertEqual(t.stop(), first)  # 冻结，不再累加

    def test_timer_as_context_manager(self):
        clk = FakeClock()
        with StepTimer(clock=clk) as t:
            clk.advance(1.5)
        self.assertAlmostEqual(t.duration, 1.5, places=3)

    def test_logger_elapsed_uses_clock(self):
        clk = FakeClock()
        log = StepLogger(sink=CollectSink(), clock=clk)
        clk.advance(10.0)
        self.assertAlmostEqual(log.elapsed(), 10.0, places=3)


class TestSummary(unittest.TestCase):
    def setUp(self):
        self.sink = CollectSink()
        self.log = StepLogger(prefix="GH", sink=self.sink, timefn=False)

    def test_summary_has_result_first(self):
        self.log.summary({"result": "SUCCESS", "attempt": "2/8"})
        self.assertIn("result=SUCCESS", self.sink.lines[0])

    def test_summary_failed_step_and_reason(self):
        self.log.summary({
            "result": "FAIL",
            "failed_step": "SESSION_SAVE",
            "reason": "no session cookie",
        })
        blob = self.sink.text()
        self.assertIn("failed_step=SESSION_SAVE", blob)
        self.assertIn("reason=no session cookie", blob)

    def test_summary_skips_empty_and_none(self):
        self.log.summary({"result": "FAIL", "reason": None, "code": ""})
        blob = self.sink.text()
        self.assertNotIn("reason=", blob)
        self.assertNotIn("code=", blob)

    def test_summary_duration_formatted(self):
        self.log.summary({"result": "SUCCESS", "duration": 142.63})
        self.assertIn("duration=142.6s", self.sink.text())

    def test_summary_plaintext_not_redacted(self):
        # PRD R7：email / username / code 明文出现，不脱敏。
        self.log.summary({
            "result": "SUCCESS",
            "email": "alice@outlook.com",
            "username": "bluewolf5821",
            "code": "03819224",
        })
        blob = self.sink.text()
        self.assertIn("alice@outlook.com", blob)
        self.assertIn("bluewolf5821", blob)
        self.assertIn("03819224", blob)
        # 确保没有星号脱敏痕迹。
        self.assertNotIn("***", blob)


class TestPlaintextStep(unittest.TestCase):
    def test_step_message_plaintext(self):
        sink = CollectSink()
        log = StepLogger(sink=sink)
        log.step("EMAIL_CODE_WAIT", "OK", "code=03819224 (via Graph API)")
        self.assertIn("03819224", sink.lines[0])


class TestTimestamp(unittest.TestCase):
    def test_step_line_has_timestamp_prefix(self):
        sink = CollectSink()
        log = StepLogger(prefix="GH", sink=sink, timefn=lambda: "03:57:00")
        log.step("FORM_FILL", "OK", "x")
        self.assertTrue(sink.lines[0].startswith("03:57:00 [GH][FORM_FILL"))

    def test_detail_line_has_timestamp_prefix(self):
        sink = CollectSink()
        log = StepLogger(prefix="GH", sink=sink, timefn=lambda: "03:57:00")
        log.detail("goto retry 1/4")
        self.assertTrue(sink.lines[0].startswith("03:57:00   "))

    def test_summary_lines_have_timestamp_prefix(self):
        sink = CollectSink()
        log = StepLogger(prefix="GH", sink=sink, timefn=lambda: "03:57:00")
        log.summary({"result": "SUCCESS", "email": "a@b.com"})
        for line in sink.lines:
            self.assertTrue(line.startswith("03:57:00 "))

    def test_timefn_false_disables_timestamp(self):
        sink = CollectSink()
        log = StepLogger(prefix="GH", sink=sink, timefn=False)
        log.step("FORM_FILL", "OK", "x")
        self.assertTrue(sink.lines[0].startswith("[GH][FORM_FILL"))

    def test_default_timestamp_is_hhmmss(self):
        # 默认 timefn 应产出 HH:MM:SS 形态前缀。
        import re
        sink = CollectSink()
        log = StepLogger(prefix="GH", sink=sink)
        log.step("FORM_FILL", "OK")
        self.assertRegex(sink.lines[0], r"^\d{2}:\d{2}:\d{2} \[GH\]")


if __name__ == "__main__":
    unittest.main()

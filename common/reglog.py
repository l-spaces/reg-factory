# -*- coding: utf-8 -*-
"""
轻量注册流程日志器（供 register_github.py 等使用）。

设计目标（见 .trellis/tasks/07-02-github-registration-logging/prd.md）:
  - 不引重型依赖，纯同步、可单元测试。
  - sink 可注入：默认打到 stdout(print)，测试时收集到 list，不触发浏览器/网络。
  - 固定宽度对齐输出：`[GH][STEP_NAME    ] STATUS   说明 (x.xs)`。
  - STATUS 词表固定：START / OK / WARN / FAIL / SKIP。
  - 相对耗时结尾（不打绝对时间戳）。
  - 明文不脱敏：email/username/password/launch code 原样输出，由调用方决定内容。

用法:
    log = StepLogger(prefix="GH")
    log.step("FORM_FILL", "START")
    ...
    log.step("FORM_FILL", "OK", "email✓ pass✓ user✓", dur=4.2)
    # 输出: 03:57:00 [GH][FORM_FILL       ] OK    email✓ pass✓ user✓ (4.2s)

每行以本地时间戳 HH:MM:SS 开头（timefn=False 可关闭；测试可注入固定值）。

阶段耗时的两种记法:
  1. 手动传 dur=<秒>。
  2. 用 log.timer("ARKOSE_SOLVE") 上下文/句柄自动计时（见 StepTimer）。
"""

import time

# PRD R2：稳定阶段名（供调用方引用，避免拼写漂移）。
STEPS = (
    "PREPARE_ACCOUNT",
    "BROWSER_OPEN",
    "SIGNUP_GOTO",
    "FORM_FILL",
    "COUNTRY_SELECT",
    "ARKOSE_SETTLE",
    "VERIFY_TRIGGER",
    "VERIFY_ROUTE",
    "ARKOSE_SOLVE",
    "EMAIL_CODE_WAIT",
    "EMAIL_CODE_SUBMIT",
    "SESSION_SAVE",
    "ACCOUNT_STORE_MARK",
    "CLEANUP",
)

# PRD R3：合法 STATUS 词表。
STATUSES = ("START", "OK", "WARN", "FAIL", "SKIP")

# 对齐宽度：取所有阶段名 + "SUMMARY" 的最长者，保证前缀列对齐。
_STEP_WIDTH = max(len(s) for s in STEPS + ("SUMMARY",))
# STATUS 也左对齐到最长者宽度，便于说明列对齐。
_STATUS_WIDTH = max(len(s) for s in STATUSES)


def _default_sink(line):
    print(line)


class StepLogger:
    """注册流程日志器。所有输出经 sink，一行一条。"""

    def __init__(self, prefix="GH", sink=None, clock=None, timefn=None):
        """
        prefix: 方括号前缀（如 "GH" -> "[GH]"）。
        sink:   接收单行字符串的可调用对象；默认 print。测试时传收集器。
        clock:  返回单调时间(秒)的可调用；默认 time.time。便于测试注入。
        timefn: 返回时间戳字符串（如 "03:57:00"）的可调用；默认取本地
                时间 HH:MM:SS。测试时可注入固定值。传 False 关闭时间戳。
        """
        self.prefix = prefix
        self._sink = sink or _default_sink
        self._clock = clock or time.time
        if timefn is False:
            self._timefn = None
        else:
            self._timefn = timefn or (lambda: time.strftime("%H:%M:%S"))
        self._start = self._clock()

    def _line(self, body):
        """给正文加时间戳前缀后送 sink。"""
        if self._timefn is not None:
            self._sink(f"{self._timefn()} {body}")
        else:
            self._sink(body)

    def elapsed(self):
        """自 logger 创建以来的总耗时(秒)。"""
        return self._clock() - self._start

    def _fmt_dur(self, dur):
        if dur is None:
            return ""
        return f"({dur:.1f}s)"

    def step(self, name, status, msg="", dur=None):
        """输出一条阶段日志。

        name:   阶段名（建议取自 STEPS，但不强制，未知名也会输出以免丢日志）。
        status: START/OK/WARN/FAIL/SKIP 之一；非法值原样输出但不崩。
        msg:    说明文字（明文，不脱敏）。
        dur:    该阶段耗时(秒)，None 则不打耗时。
        """
        name = str(name)
        status = str(status)
        step_col = name.ljust(_STEP_WIDTH)
        status_col = status.ljust(_STATUS_WIDTH)
        parts = [f"[{self.prefix}][{step_col}]", status_col]
        if msg:
            parts.append(str(msg))
        dur_str = self._fmt_dur(dur)
        if dur_str:
            parts.append(dur_str)
        self._line(" ".join(parts).rstrip())

    def detail(self, msg):
        """阶段内的次要说明，缩进两格，不抢主线。"""
        self._line("  " + str(msg))

    def timer(self, name):
        """返回一个 StepTimer，用于自动测量某阶段耗时。

        用法:
            t = log.timer("ARKOSE_SOLVE")
            ... 干活 ...
            log.step("ARKOSE_SOLVE", "OK", "solved", dur=t.stop())
        或作为上下文管理器（退出时不自动打日志，只提供 .duration）:
            with log.timer("X") as t: ...
            log.step("X", "OK", dur=t.duration)
        """
        return StepTimer(self._clock)

    def summary(self, data):
        """输出贯穿式 summary（PRD R6）。

        data: dict，常见键 result/email/username/attempt/failed_step/
              reason/duration/profile/pid/final_url。缺省键跳过。
        输出为多行：首行 result，其余键值缩进对齐，全部明文。
        """
        step_col = "SUMMARY".ljust(_STEP_WIDTH)
        head = f"[{self.prefix}][{step_col}]"
        result = data.get("result", "UNKNOWN")
        self._line(f"{head} result={result}")
        # 固定顺序输出关键字段，缺省跳过。
        order = (
            "attempt", "failed_step", "reason",
            "email", "username", "code",
            "duration", "profile", "pid", "final_url",
        )
        indent = " " * (len(head) + 1)
        for key in order:
            if key == "result":
                continue
            if key in data and data[key] not in (None, ""):
                val = data[key]
                if key == "duration" and isinstance(val, (int, float)):
                    val = f"{val:.1f}s"
                self._line(f"{indent}{key}={val}")


class StepTimer:
    """轻量计时句柄。stop() 返回耗时并冻结；亦可作为 with 上下文。"""

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self._start = self._clock()
        self.duration = None

    def stop(self):
        if self.duration is None:
            self.duration = self._clock() - self._start
        return self.duration

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

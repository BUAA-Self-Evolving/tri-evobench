"""Bounded deterministic retry messages for multi-Interaction Episodes."""


class BoundedRetryUserSimulator:
    _MESSAGES = {
        "fail": (
            "上一轮提交未通过当前任务的确定性验证。请重新检查任务要求、输出格式和关键步骤，"
            "然后重新给出完整提交。验证器的隐藏内容不会提供。"
        ),
        "timeout": (
            "上一轮执行超时。请减少不必要的计算或阻塞步骤，并重新给出符合任务输出格式的完整提交。"
        ),
    }

    def next_request(self, *, result_category: str, attempt: int) -> str:
        if result_category not in self._MESSAGES:
            raise ValueError(f"no retry message for result category {result_category!r}")
        return f"第 {attempt} 次提交反馈：{self._MESSAGES[result_category]}"

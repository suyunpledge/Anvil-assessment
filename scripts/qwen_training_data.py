"""Synthetic Qwen Coder examples using Anvil's real prompts and tool schema."""
from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.paths import ensure_core_on_path

ensure_core_on_path()
from engine import REGISTRY, WorkOrderStateMachine
from mythos_adapter import MythosAdapter
from mythos_core.profiles import QWEN_CODER, PROFILE_BY_MODEL, get_profile


@dataclass(frozen=True)
class Case:
    name: str
    task: str
    source: str
    old: str = ""
    new: str = ""
    answer: str = ""
    feedback: str = ""
    target: str = "helpers.py"

    def prompt(self, adapter):
        sm = object.__new__(WorkOrderStateMachine)
        sm.ad = adapter
        sm.wo = SimpleNamespace(task=self.task, target=self.target)
        sm._visual_ok = False
        sm.context_provider = None
        messages = [
            {"role": "system", "content": sm._sys_edit(self.target, bool(self.feedback))},
            {"role": "user", "content": sm._user_edit(None, {"target_source": self.source}, self.feedback)},
        ]
        return messages, adapter.tools_for("edit", REGISTRY)

    def completion(self):
        if self.answer:
            return {"role": "assistant", "content": self.answer}
        return {"role": "assistant", "content": "", "tool_calls": [{"type": "function",
            "function": {"name": "apply_patch", "arguments": {
                "path": self.target, "old_string": self.old, "new_string": self.new}}}]}

    def expected_source(self):
        if self.answer:
            return self.source
        if not self.old or self.source.count(self.old) != 1:
            raise ValueError(f"{self.name}: expected one text anchor")
        return self.source.replace(self.old, self.new, 1)


def adapter_for_data(spec=QWEN_CODER):
    return MythosAdapter(model=spec.model, spec=spec,
                         profile_path=str(ROOT / ".runtime" / "qwen-training-data.json"))


def training_cases():
    cases = []
    helpers = [
        ("is_even", "value: int", "bool", "判断整数是否为偶数", "value % 2 == 0"),
        ("trim_label", "label: str", "str", "删除字符串两端空白", "label.strip()"),
        ("square", "value: int", "int", "返回整数的平方", "value * value"),
        ("join_words", "words: list[str]", "str", "用单个空格连接字符串列表", '" ".join(words)'),
        ("count_nonempty", "words: list[str]", "int", "计算非空字符串的数量", "sum(bool(word) for word in words)"),
        ("last_or_zero", "values: list[int]", "int", "返回最后一个元素，空列表返回0", "values[-1] if values else 0"),
        ("absolute_total", "values: list[int]", "int", "返回元素绝对值的总和", "sum(abs(value) for value in values)"),
        ("initials", "words: list[str]", "str", "拼接非空单词的首字符", '"".join(word[0] for word in words if word)'),
        ("celsius", "fahrenheit: float", "float", "将华氏温度转换为摄氏温度", "(fahrenheit - 32) * 5 / 9"),
        ("nonnegative", "value: int", "int", "负数返回0，其余返回原值", "max(0, value)"),
        ("tail", "values: list[int]", "list[int]", "返回去掉首元素后的列表", "values[1:]"),
        ("ends_period", "text: str", "bool", "判断文本是否以英文句号结尾", 'text.endswith(".")'),
    ]
    neighbors = "\n\ndef identity(number: int) -> int:\n    return number\n"
    for name, args, result, instruction, expression in helpers:
        old = f'    raise NotImplementedError("{name}")'
        code = f"def {name}({args}) -> {result}:\n{old}\n"
        cases.append(Case(name, f"实现 {name}：{instruction}。保留类型注解和其他函数。",
                          code + neighbors, old, f"    return {expression}", target="helpers.py"))
        cases.append(Case(f"{name}-existing", f"请补齐 {name} 的函数体。需求是{instruction}，只修改此函数。",
                          neighbors + "\n" + code, old, f"    return {expression}", target="logic/core.py"))
    for idx, (old, new) in enumerate([
        ("返回名称", "返回去掉两端空白的名称。"),
        ("检查标识", "检查标识是否只包含数字。"),
        ("处理项目", "转换项目名称，空名称保持为空字符串。"),
        ("获取总计", "计算订单金额，结果以元为单位。"),
        ("生成标签", "返回用于列表显示的短标签。"),
        ("转换文本", "将输入文本转换为小写。"),
    ]):
        for target, doc in (("labels.py", '"""'), ("util/names.py", "'''")):
            source = f"def label(name: str) -> str:\n    {doc}{old}{doc}\n    return name\n"
            cases.append(Case(f"doc-{idx}-{target}", f"把 label 的文档改为「{new}」，只改文档。",
                              source + neighbors, old, new, target=target))
    repairs = [
        ("limit_size", "size: int", "int", "return min(szie, 20)", "return min(size, 20)",
         "NameError: name 'szie' is not defined", "返回不超过20的大小"),
        ("is_ready", "state: str", "bool", 'return state != "ready"', 'return state == "ready"',
         "AssertionError: is_ready('ready') 应返回 True", "仅在状态为 ready 时返回 True"),
        ("increment", "value: int", "int", "return value +", "return value + 1",
         "SyntaxError: invalid syntax at return value +", "将整数加1"),
        ("total", "values: list[int]", "int", "return sum(values", "return sum(values)",
         "SyntaxError: '(' was never closed", "计算整数列表总和"),
        ("nonempty", "text: str", "bool", "return len(text) == 0", "return bool(text)",
         "AssertionError: nonempty('x') 应返回 True", "判断字符串是否非空"),
        ("next_page", "page: int", "int", "return current + 1", "return page + 1",
         "NameError: name 'current' is not defined", "返回下一页页码"),
    ]
    for name, args, result, old, new, feedback, task in repairs:
        code = f"def {name}({args}) -> {result}:\n    {old}\n"
        cases.append(Case(name, task, code + neighbors, old, new, feedback=feedback))
        cases.append(Case(f"{name}-repair", f"修复 {name}，{task}。保留无关函数。",
                          neighbors + "\n" + code, old, new, feedback=feedback, target="src/logic.py"))
    for idx, (task, answer) in enumerate([
        ("按新版规则修改价格。", "请提供新版价格规则和一个输入输出示例。"),
        ("让它返回正确的默认值。", "请指定需要修改的函数和期望的默认值。"),
        ("把标签改成我们约定的新名字。", "请提供标签的新名字。"),
        ("按最新规范处理空结果。", "请说明最新规范要求空结果返回什么。"),
        ("换成新的舍入方法。", "请说明新的舍入方式及保留的小数位数。"),
        ("改成昨天说的格式。", "请贴出目标格式或一条输入输出示例。"),
    ]):
        cases.append(Case(f"clarify-{idx}", task, neighbors, answer=answer))
    cases.append(Case("remove-debug", "移除 debug 中的调试输出，保留返回值。",
                      "def debug(n: int) -> int:\n    print(n)\n    return n\n", "    print(n)\n", ""))
    return cases


def heldout_cases():
    return [
        Case("doc-non-ascii", "只把 score 的文档字符串替换为「返回积分，最低为零。」。",
             'def score(points: int) -> int:\n    """积分结果"""\n    return max(points, 0)\n',
             "积分结果", "返回积分，最低为零。", target="src/scoring.py"),
        Case("doc-quoted", '只把 describe 的文档改为「默认使用 "draft" 模式。」。',
             'def describe() -> str:\n    """描述当前模式。"""\n    return "draft"\n',
             "描述当前模式。", '默认使用 "draft" 模式。', target="notes.py"),
        Case("clamp-boundary", "修复 within，边界值应包含 limit，其他逻辑保持。",
             "def within(value: int, limit: int) -> bool:\n    return 0 <= value < limit\n",
             "0 <= value < limit", "0 <= value <= limit", feedback="AssertionError: within(4, 4) 应为 True"),
        Case("wrong-name", "修复 transport_cost 中未定义的变量，返回距离乘单价。",
             "def transport_cost(distance: int, rate: int) -> int:\n    return dist * rate\n",
             "dist * rate", "distance * rate", feedback="NameError: name 'dist' is not defined"),
        Case("syntax-colon", "修复 clear_line 的语法错误，保持行为与签名不变。",
             'def clear_line(line: str) -> str\n    return line.rstrip("\\n")\n',
             "def clear_line(line: str) -> str", "def clear_line(line: str) -> str:",
             feedback="SyntaxError: expected ':' (line 1)"),
        Case("remove-diagnostic", "删除 selected 中的临时日志，保留原返回值。",
             'def selected(flag: bool) -> bool:\n    print("trace", flag)\n    return flag\n',
             '    print("trace", flag)\n', ""),
        Case("minimal-neighbor", "只修正 width，将 cm 转成 mm。height 不变。",
             "def width(cm: int) -> int:\n    return cm * 100\n\ndef height(cm: int) -> int:\n    return cm * 100\n",
             "def width(cm: int) -> int:\n    return cm * 100", "def width(cm: int) -> int:\n    return cm * 10"),
        Case("missing-contract", "按未提供的结算政策更改 fee。请先问清规则。",
             "def fee(total: int) -> int:\n    return total // 10\n", answer="请提供结算政策及期望的费用计算规则。"),
    ]


def score(case, result):
    if case.answer:
        text = result.content or ""
        return result.kind == "no_call" and bool(text.strip()) and ("请" in text or "?" in text or "？" in text)
    if result.kind != "tool_call" or len(result.calls) != 1:
        return False
    call = result.calls[0]
    args = call["args"]
    anchor = args.get("old_string")
    if (call["name"] != "apply_patch" or args.get("path") != case.target
            or not anchor or case.source.count(anchor) != 1):
        return False
    try:
        actual = ast.parse(case.source.replace(anchor, args["new_string"], 1))
    except SyntaxError:
        return False
    return ast.dump(actual) == ast.dump(ast.parse(case.expected_source()))


def evaluate_cases(adapter, cases):
    rows = []
    for case in cases:
        started = time.monotonic()
        messages, _ = case.prompt(adapter)
        result = adapter.fill_slot("edit", messages, REGISTRY, n=1, temp=0)
        row = {"case": case.name, "passed": score(case, result), "kind": result.kind,
               "calls": result.calls, "text": result.content, "error": result.error,
               "seconds": round(time.monotonic() - started, 3)}
        rows.append(row)
        print(case.name, row["passed"], row["kind"], flush=True)
    return rows


if __name__ == "__main__":
    train, heldout = training_cases(), heldout_cases()
    assert not {row.name for row in train} & {row.name for row in heldout}
    for row in train + heldout:
        ast.parse(row.expected_source(), filename=row.target)
    print(f"Validated {len(train)} training examples and {len(heldout)} held-out cases.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", help="Ollama model to evaluate; repeat to compare")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--use-model-profile", action="store_true",
                        help="use each registered model's actual IDE prompts instead of a shared prompt")
    opts = parser.parse_args()
    if bool(opts.model) != bool(opts.report):
        parser.error("--model and --report must be used together")
    if opts.use_model_profile and (not opts.model or any(model not in PROFILE_BY_MODEL for model in opts.model)):
        parser.error("--use-model-profile needs registered --model tags")
    if opts.model:
        results = {}
        for model in opts.model:
            print(f"Evaluating {model}", flush=True)
            adapter = adapter_for_data(get_profile(model) if opts.use_model_profile else QWEN_CODER)
            adapter.model = model
            # Unload this evaluation model at the end of each comparison.
            transport = adapter.transport
            try:
                results[model] = evaluate_cases(adapter, heldout)
                print(f"{model}: {sum(row['passed'] for row in results[model])}/{len(heldout)}", flush=True)
            finally:
                transport.chat({"model": model, "messages": [], "keep_alive": 0, "stream": False}, timeout=15)
        opts.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

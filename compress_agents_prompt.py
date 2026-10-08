#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词压缩流水线（Compression-TGD）
==================================

把 TextGrad 的「文本梯度下降」反过来用：**目标不是提升准确率，而是在行为不劣化
的前提下最小化提示词字符数**。

为什么不能直接用 TextGrad
-------------------------
参考库 `/Users/cdyanglijun/codex_test/textgrad` 是**增长**优化器，三处默认与压缩冲突：

1. **默认方向相反**。实测 `mmlu_ml_prompt_opt_results.json` 的 prompt_history：
   98 → 2502 → 4715 字符（净 +4617）。它只会加规则，不会删。
2. **「已达标则不反馈」会让压缩在第一轮就卡死**。
   `textgrad/autograd/llm_backward_prompts.py:20`：
     "If a variable is already working well ... you should not give feedback."
   本项目 baseline 已 15/18~18/18，backward engine 会判定"无需改进" → 梯度恒为零。
   该常量在 llm_ops.py 等 8 处被消费，必须**整体替换**。
3. **TGD 每步整段重写，会静默丢语义**（optimizer.py:181 从 <IMPROVED_VARIABLE>
   直接取新值替换，无 diff、无保真校验）。压缩是减法任务，必须强制它输出
   **逐条删除清单**，否则无法归因是"压掉了措辞"还是"删掉了 R4"。

本脚本的设计
------------
- **不依赖 textgrad 包**：只借用它的思想（textual gradient + constrained update），
  用 DeepSeek 直接实现。这样避免引入 openai/datasets/litellm 等重依赖，
  也避免它的增长偏置需要到处打补丁。
- **五阶段**（对应 README 的方法设计）：
    Stage 0 基线冻结 → Stage 1 压缩梯度 → Stage 2 约束重写
    → Stage 3 非劣门禁（掉分即回滚） → Stage 4 收敛
- **复用 agents_selftest.py 的判据**：`Verdict.reasons` 本身就是自然语言 loss，
  正是 TextGrad 的 textual gradient 格式，不需要另写 loss 函数。

硬约束（路线 A：只压缩措辞、删冗余解释）
--------------------------------------
  不得新增规则；第七节格式一字不改；R1–R6/R2b/C1–C5/U1–U3/F1–F4 全部保留；
  硬规则语义强度不得弱化；只删元层面说明、重复表述、修辞过渡。

用法
----
  python3 compress_agents_prompt.py --dry-run          # 只做静态校验，不调 API
  python3 compress_agents_prompt.py --propose          # 只跑 Stage 1，输出删除建议
  python3 compress_agents_prompt.py --full             # 完整流水线（含 API 验证）
  python3 compress_agents_prompt.py --verify FILE      # 只验证某个候选文件是否非劣
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 复用自测脚本的判据、用例与模型调用——避免第二套实现漂移
import agents_selftest as st  # noqa: E402

SOURCE = HERE / "agents-prompt.md"
CANDIDATE = HERE / "agents-prompt.compressed.md"
WORKDIR = HERE / "compress_work"

# ---------------------------------------------------------------------------
# 硬约束：路线 A 的护栏（注入 <CONSTRAINTS>，对应 textgrad 的 constraint_text）
# ---------------------------------------------------------------------------
ROUTE_A_CONSTRAINTS = [
    "只允许删除或合并——不得新增任何规则、示例、章节或例子。",
    # 注意：不要写死章节号——「输出格式」在原版是第七节、在压缩版是第六节。
    # 写死会在下一轮迭代里指错对象（实测：梯度引擎据此输出"保留第六节"，
    # 而它正在压缩的是把该节编为第七节的原文）。
    "必须完整保留『输出格式』那一节的全部区段标题：【结论】【依据】【假设】"
    "【判据】【未验证 / 不知道】【需要你确认】，名称、顺序与括号形式一字不改。",
    "必须保留全部规则标识（R1–R6、R2b、C1–C5、U1–U3、F1–F4），"
    "不得删除、重编号或改变其含义。",
    "不得改变任何硬规则的语义强度：「必须/不得/禁止」不得弱化为「建议/尽量/可以」。",
    "可以删除的仅限：元层面说明（本文件如何被加载、为什么这样写、引用来源的解释）、"
    "同一规则的重复表述、修辞性过渡句。",
    "不得改动任何与判据强耦合的词：『需要你确认』『判据』『未验证』"
    "（删掉它们会让输出不再可机检）。",
]

# 静态校验：规则标识与格式标题必须 100% 保留
RULE_IDS = ["R1", "R2", "R2b", "R3", "R4", "R5", "R6",
            "C1", "C2", "C3", "C4", "C5",
            "U1", "U2", "U3", "F1", "F2", "F3", "F4"]
FORMAT_MARKERS = ["【结论】", "【依据】", "【假设】", "【判据】",
                  "【未验证", "【需要你确认】"]
# 与 agents_selftest 判据耦合的词（路线 A 下不得丢失）
COUPLED_WORDS = ["需要你确认", "判据", "未验证", "【依据】", "无法确定"]


# ---------------------------------------------------------------------------
# Stage 1：压缩梯度引擎（替换 textgrad 的 BACKWARD_SYSTEM_PROMPT）
# ---------------------------------------------------------------------------
COMPRESSION_GRADIENT_SYSTEM = """\
You are the COMPRESSION GRADIENT ENGINE for a system prompt.

The variable is a system prompt that constrains an AI agent's reasoning behavior.
Its current job is WORKING: it already induces the desired behavior. Your job is
NOT to improve its behavior. Your job is to find text that can be REMOVED without
changing any behavior the prompt induces.

OBJECTIVE: minimize the prompt's length WITHOUT changing any behavior it induces.

Apply the prompt's own ladder test to every sentence:
  "If I delete this sentence, does the induced behavior still hold?"
  - If YES  -> the sentence is decoration or duplication -> CUT or MERGE.
  - If NO   -> it is load-bearing -> KEEP, and state the concrete behavior it causes.

Output EXACTLY one line per candidate, in this format:
  [KEEP|CUT|MERGE] <first 24 chars of the passage> | <reason> | <chars saved>

Rules:
- CUT only when deleting changes NO induced behavior.
- MERGE when two or more passages state the SAME rule (list all locations).
- KEEP if load-bearing; name the concrete behavior it produces.
- NEVER propose a rewritten prompt. That is the optimizer's job, not yours.
- Pay special attention to: meta-commentary about how the file is loaded, restatements
  of the same rule in different words, rhetorical transitions, and motivational prose.
- Hard constraints (violating any of these makes a CUT invalid):
{constraints}

Report the total characters saved at the end as: TOTAL_SAVED: <number>
"""

# ---------------------------------------------------------------------------
# Stage 2：约束重写（对应 textgrad 的 TGD_PROMPT_PREFIX + CONSTRAINTS）
# ---------------------------------------------------------------------------
COMPRESSION_OPTIMIZER_SYSTEM = (
    "You are part of an optimization system that COMPRESSES text (i.e., a variable). "
    "You will receive compression feedback and must apply it to the variable. "
    "You are strictly forbidden from adding new content: this is a deletion/merging task. "
    "Every behavioral rule must survive with identical meaning and identical strength. "
    "You MUST send the improved variable between <IMPROVED_VARIABLE> and "
    "</IMPROVED_VARIABLE> tags. The text between the tags will directly replace the variable."
)

COMPRESSION_OPTIMIZER_PROMPT = """\
Here is the role of the variable you will compress: <ROLE>{role}</ROLE>.

The variable is the text within the following span:
<VARIABLE>
{variable}
</VARIABLE>

Here is the compression feedback we got for the variable:

<FEEDBACK>
{feedback}
</FEEDBACK>

You must follow the following constraints:

<CONSTRAINTS>
{constraints}
</CONSTRAINTS>

Compress the variable by applying the feedback. Delete and merge only.
Then output, in this exact order:

<DELETIONS>
- <the deleted passage, quoted> | <chars saved> | <why it is safe to delete>
</DELETIONS>

<MERGES>
- <which passages were merged into which> | <chars saved>
</MERGES>

<RISK>
- <any rule whose meaning you are unsure you preserved; write "none" if none>
</RISK>

<IMPROVED_VARIABLE>
THE FULL COMPRESSED VARIABLE GOES HERE
</IMPROVED_VARIABLE>
"""


# ---------------------------------------------------------------------------
# 静态校验：不调 API 就能查出"压过头"
# ---------------------------------------------------------------------------
@dataclass
class StaticCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _rule_token_re(rule: str) -> re.Pattern[str]:
    """匹配规则标识本身，**与排版无关**。

    修因（首次 dry-run 实测）：我第一版写成 `\\*\\*R1\\b`，即要求 Markdown 粗体，
    结果把**原版的 `### R1. ...` 标题式排版**判成"规则标识全部丢失"（1/19）——
    这正是本项目反复出现的"把格式当成语义"的同一个错误，只是这次犯在校验器里。
    规则是否存在是**语义事实**，与它是标题、粗体还是纯文本无关。
    """
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(rule)}(?![A-Za-z0-9])")


def static_check(text: str) -> StaticCheck:
    """检查压缩候选是否违反路线 A 的硬约束。

    这是**第一道门禁**，成本为零，能挡掉绝大多数"压过头"的候选：
    规则标识丢失、格式标题被改、语义强度弱化、耦合词丢失。
    """
    problems: list[str] = []

    missing_rules = [r for r in RULE_IDS if not _rule_token_re(r).search(text)]
    if missing_rules:
        problems.append(f"规则标识丢失：{missing_rules}")

    missing_fmt = [m for m in FORMAT_MARKERS if m not in text]
    if missing_fmt:
        problems.append(f"输出格式标题丢失：{missing_fmt}")

    missing_coupled = [w for w in COUPLED_WORDS if w not in text]
    if missing_coupled:
        problems.append(f"与判据耦合的词丢失（会让输出不可机检）：{missing_coupled}")

    # 语义强度弱化：规则标识所在行/句里出现弱化词（与排版无关）
    weakened = []
    for rule in RULE_IDS:
        for m in _rule_token_re(rule).finditer(text):
            seg = text[m.start(): m.start() + 90]
            if re.search(r"(建议|尽量|可以尝试|最好|推荐)", seg):
                weakened.append(rule)
    if weakened:
        problems.append(f"疑似弱化硬规则语义强度：{sorted(set(weakened))}")

    # 新增内容信号：出现原文没有的"应当新增"式表述
    added = [w for w in ["新增", "补充说明如下", "此外还需", "另外要"] if w in text]
    if added:
        problems.append(f"疑似新增内容（路线 A 禁止）：{added}")

    return StaticCheck(
        ok=not problems,
        problems=problems,
        metrics={
            "chars": len(text),
            "rules_found": len(RULE_IDS) - len(missing_rules),
            "format_found": len(FORMAT_MARKERS) - len(missing_fmt),
        },
    )


def semantic_completeness(text: str) -> tuple[int, int, list[str]]:
    """逐条检查 17 个规则要点是否仍在（比标识更细的语义层校验）。

    返回 (命中数, 总数, 缺失说明)。要点清单来自对原文的人工提炼。
    """
    checks = {
        "R1": ["前提", "符号", "中途"],
        "R2": ["引用", "推导", "假设", "出处", "作弊"],
        "R2b": ["内部编号", "证据来源", "例外"],
        "R3": ["可检验", "歧义"],
        "R4": ["证明它错", "反例条件", "删除"],
        "R5": ["描述", "规范", "约定"],
        "R6": ["具体用法", "可观察行为", "停下来提问"],
        "C1": ["重述", "从前提得出"],
        "C2": ["删掉这句", "装饰", "承重"],
        "C3": ["结论崩溃", "依赖"],
        "C4": ["适用范围", "不知道"],
        "C5": ["凭记忆编造", "未验证", "应该没问题"],
        "U1": ["材料不足", "推理不确定", "概念不清"],
        "U2": ["默认值", "此假定未验证"],
        "U3": ["我不知道", "获取路径"],
        "停止条件": ["无用法术语", "互相冲突", "无法验证的关键假设", "风险差异"],
        "F1-F4": ["形式合规", "短而可核验", "过程可检查", "越界"],
    }
    total = len(checks)
    hit = 0
    missing: list[str] = []
    for rule, keys in checks.items():
        miss = [k for k in keys if k not in text]
        if miss:
            missing.append(f"{rule} 缺 {miss}")
        else:
            hit += 1
    return hit, total, missing


# ---------------------------------------------------------------------------
# Stage 1 / Stage 2 的 LLM 调用（复用 agents_selftest 的 call_llm）
# ---------------------------------------------------------------------------
def compression_gradient(api_key: str, model: str, variable: str) -> str:
    constraints = "\n".join(f"  - {c}" for c in ROUTE_A_CONSTRAINTS)
    system = COMPRESSION_GRADIENT_SYSTEM.format(constraints=constraints)
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content":
            "Here is the variable to analyze for compression:\n\n"
            f"<VARIABLE>\n{variable}\n</VARIABLE>\n\n"
            "Produce the KEEP/CUT/MERGE line list now."},
    ]
    return st.call_llm(msgs, api_key, model=model, max_tokens=4000)


def constrained_rewrite(api_key: str, model: str, variable: str, feedback: str) -> str:
    constraints = "\n".join(f"Constraint {i+1}: {c}"
                            for i, c in enumerate(ROUTE_A_CONSTRAINTS))
    prompt = COMPRESSION_OPTIMIZER_PROMPT.format(
        role=("a system prompt that constrains an AI agent's reasoning discipline; "
              "compression must preserve every behavioral rule and the output format"),
        variable=variable,
        feedback=feedback,
        constraints=constraints,
    )
    msgs = [
        {"role": "system", "content": COMPRESSION_OPTIMIZER_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    out = st.call_llm(msgs, api_key, model=model, max_tokens=8000)
    m = re.search(r"<IMPROVED_VARIABLE>(.*?)</IMPROVED_VARIABLE>", out, re.S)
    if not m:
        raise st.LLMError("优化器未按要求输出 <IMPROVED_VARIABLE> 标签")
    return m.group(1).strip(), out


# ---------------------------------------------------------------------------
# Stage 3：非劣门禁（对应 textgrad 的 run_validation_revert）
# ---------------------------------------------------------------------------
def behavioral_pass_rate(
    api_key: str, model: str, prompt_text: str, cases: list, repeats: int,
    save_tag: str | None = None, verbose: bool = True, save_raw: bool = False,
) -> tuple[float, dict[str, float]]:
    """跑全部用例，返回 (总通过率, {cid: 通过率})。"""
    per_case: dict[str, float] = {}
    for case in cases:
        n = 0
        for _ in range(repeats):
            v, text, _ = st.run_case(case, api_key, model, prompt_text, save_raw,
                                     tag=save_tag)
            n += int(v.passed)
        per_case[case.cid] = n / repeats
        if verbose:
            print(f"      {case.cid:<6} {n}/{repeats}")
    total = sum(per_case.values()) / len(per_case)
    return total, per_case


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def cmd_dry_run() -> int:
    print("== 静态校验（不调 API）==\n")
    src = SOURCE.read_text(encoding="utf-8")
    for path in [SOURCE, CANDIDATE]:
        if not path.is_file():
            print(f"  [跳过] {path.name} 不存在")
            continue
        text = path.read_text(encoding="utf-8")
        sc = static_check(text)
        hit, total, missing = semantic_completeness(text)
        print(f"-- {path.name}：{len(text)} 字符")
        print(f"   静态约束：{'通过' if sc.ok else '违规'}")
        for p in sc.problems:
            print(f"     ✗ {p}")
        print(f"   语义完整度：{hit}/{total}" + (f"  缺 {missing}" if missing else ""))
        print(f"   规则标识 {sc.metrics['rules_found']}/{len(RULE_IDS)}，"
              f"格式标题 {sc.metrics['format_found']}/{len(FORMAT_MARKERS)}")
        print()
    return 0


def cmd_verify(path_str: str, api_key: str | None, model: str,
               repeats: int, margin: float, only: str) -> int:
    """验证给定候选文件是否非劣于原版。"""
    cand = Path(path_str)
    if not cand.is_absolute():
        cand = HERE / cand
    if not cand.is_file():
        print(f"[错误] 找不到候选文件：{cand}", file=sys.stderr)
        return 2
    src_text = SOURCE.read_text(encoding="utf-8")
    cand_text = cand.read_text(encoding="utf-8")

    print("== 第一道门禁：静态校验 ==")
    sc = static_check(cand_text)
    hit, total, missing = semantic_completeness(cand_text)
    print(f"  静态约束：{'通过' if sc.ok else '违规'}")
    for p in sc.problems:
        print(f"    ✗ {p}")
    print(f"  语义完整度：{hit}/{total}")
    if missing:
        for m in missing:
            print(f"    ✗ {m}")
    if not sc.ok:
        print("\n结论：静态校验未通过，不必调用 API（路线 A 硬约束被破坏）")
        return 1
    print(f"\n  长度：{len(src_text)} → {len(cand_text)} 字符 "
          f"（{(len(cand_text)/len(src_text) - 1):+.1%}）\n")

    if api_key is None:
        print("（无 API key，跳过行为验证）")
        return 0

    print("== 第二道门禁：行为非劣（调 API）==")
    cases = st.CASES
    if only:
        wanted = {x.strip().lower() for x in only.split(",") if x.strip()}
        cases = [c for c in cases if c.cid.lower() in wanted]
    print(f"  A = {SOURCE.name}")
    rate_a, per_a = behavioral_pass_rate(api_key, model, src_text, cases, repeats,
                                        save_tag="orig")
    print(f"    总通过率 {rate_a:.0%}\n  B = {cand.name}")
    rate_b, per_b = behavioral_pass_rate(api_key, model, cand_text, cases, repeats,
                                        save_tag="comp")
    print(f"    总通过率 {rate_b:.0%}\n")

    print(f"  {'用例':<7}{'A':<8}{'B':<8}{'Δ':<9}判定")
    print("  " + "-" * 44)
    regressions = []
    for c in cases:
        a, b = per_a[c.cid], per_b[c.cid]
        d = b - a
        if d < -margin - 1e-9:
            note = "✗ 退化超界"
            regressions.append(c.cid)
        elif d > 1e-9:
            note = "↑ 改善"
        else:
            note = "="
        print(f"  {c.cid:<7}{f'{a:.0%}':<8}{f'{b:.0%}':<8}{f'{d:+.0%}':<9}{note}")

    n = len(cases) * repeats
    pa = st.fisher_exact_p(int(rate_b * n), n - int(rate_b * n),
                           int(rate_a * n), n - int(rate_a * n))
    print(f"\n  整体：A {rate_a:.0%} → B {rate_b:.0%}，Fisher 单侧 p={pa:.3f}")
    print(f"  退化超界：{', '.join(regressions) if regressions else '无'}")
    if regressions:
        print("\n结论：**不满足非劣**")
        return 1
    print("\n结论：满足非劣（静态 + 行为双门禁通过）")
    return 0


def cmd_propose(api_key: str, model: str) -> int:
    """Stage 1：只产出压缩梯度（删除建议），不改写。"""
    variable = SOURCE.read_text(encoding="utf-8")
    print(f"== Stage 1：压缩梯度（{len(variable)} 字符）==\n")
    grad = compression_gradient(api_key, model, variable)
    WORKDIR.mkdir(exist_ok=True)
    (WORKDIR / "gradient.txt").write_text(grad, encoding="utf-8")
    print(grad)
    print(f"\n已保存到 {WORKDIR / 'gradient.txt'}")
    m = re.search(r"TOTAL_SAVED:\s*(\d+)", grad)
    if m:
        n = int(m.group(1))
        print(f"梯度声称可省：{n} 字符（{n/len(variable):.1%}）"
              f" → 预估压缩后 {len(variable)-n} 字符")
    return 0


def cmd_full(api_key: str, model: str, repeats: int, margin: float,
             max_iters: int) -> int:
    """完整流水线：梯度 → 重写 → 静态门禁 → 行为门禁 → 回滚。"""
    WORKDIR.mkdir(exist_ok=True)
    original = SOURCE.read_text(encoding="utf-8")
    current, current_rate = original, None

    print("== Stage 0：基线冻结 ==")
    print("  A（原版）行为基线：")
    rate_a, per_a = behavioral_pass_rate(api_key, model, original, st.CASES, repeats,
                                        save_tag="orig")
    print(f"  基线总通过率：{rate_a:.0%}\n")

    history = []
    for it in range(max_iters):
        print(f"===== 迭代 {it+1}/{max_iters} =====")
        print(f"  当前提示词 {len(current)} 字符")

        print("\n  -- Stage 1：压缩梯度 --")
        grad = compression_gradient(api_key, model, current)
        (WORKDIR / f"gradient_{it+1}.txt").write_text(grad, encoding="utf-8")
        cuts = [l for l in grad.splitlines() if l.strip().startswith(("[CUT]", "[MERGE]"))]
        print(f"     候选删除/合并 {len(cuts)} 条")

        print("\n  -- Stage 2：约束重写 --")
        try:
            candidate, raw = constrained_rewrite(api_key, model, current, grad)
        except st.LLMError as exc:
            print(f"     ✗ 重写失败：{exc}")
            break
        (WORKDIR / f"candidate_{it+1}.md").write_text(candidate, encoding="utf-8")
        (WORKDIR / f"rewrite_raw_{it+1}.txt").write_text(raw, encoding="utf-8")
        print(f"     候选 {len(candidate)} 字符（{len(candidate)-len(current):+d}）")

        if len(candidate) >= len(current):
            print("     ✗ 未变短，停止迭代（优化器没有执行删除）")
            break

        print("\n  -- Stage 3a：静态门禁 --")
        sc = static_check(candidate)
        hit, total, missing = semantic_completeness(candidate)
        print(f"     静态约束：{'通过' if sc.ok else '违规'}"
              f"｜语义完整度 {hit}/{total}")
        for p in sc.problems:
            print(f"       ✗ {p}")
        if not sc.ok:
            print("     → 回滚（违反路线 A 硬约束）")
            history.append({"iter": it + 1, "action": "reject-static",
                            "chars": len(candidate)})
            continue

        print("\n  -- Stage 3b：行为门禁 --")
        rate_b, per_b = behavioral_pass_rate(api_key, model, candidate, st.CASES,
                                            repeats, save_tag="comp", verbose=False)
        regressions = [c.cid for c in st.CASES
                       if per_b[c.cid] - per_a[c.cid] < -margin - 1e-9]
        print(f"     原版 {rate_a:.0%} → 候选 {rate_b:.0%}")
        if regressions:
            print(f"     ✗ 退化超界：{regressions} → 回滚")
            history.append({"iter": it + 1, "action": "reject-behavior",
                            "chars": len(candidate), "regressions": regressions})
            continue

        print("     ✓ 通过 → 接受")
        history.append({"iter": it + 1, "action": "accept", "chars": len(candidate),
                        "rate": rate_b})
        current = candidate
        final = WORKDIR / "accepted.md"
        final.write_text(current, encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"最终：{len(original)} → {len(current)} 字符 "
          f"（{(len(current)/len(original) - 1):+.1%}）")
    (WORKDIR / "history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"接受版本：{WORKDIR / 'accepted.md'}")
    print("提示：用 --verify compress_work/accepted.md 复核后再人工读原文确认。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="提示词压缩流水线（Compression-TGD，路线 A）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="只做静态校验，不调 API")
    ap.add_argument("--propose", action="store_true", help="只跑 Stage 1 压缩梯度")
    ap.add_argument("--full", action="store_true", help="完整流水线")
    ap.add_argument("--verify", metavar="FILE", default=None, help="验证候选文件是否非劣")
    ap.add_argument("--model", default=st.DEFAULT_MODEL)
    ap.add_argument("--repeat", type=int, default=1, help="每用例跑几次（建议 3）")
    ap.add_argument("--margin", type=float, default=0.0,
                    help="允许的通过率退化幅度（0.0=一处都不许退）")
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--only", default="", help="--verify 时只跑指定用例")
    ap.add_argument("--allow-key-command", action="store_true",
                    help="允许执行 rc 里 DEEPSEEK_API_KEY 的 $(...) 命令替换来取值")
    args = ap.parse_args()

    if args.dry_run:
        return cmd_dry_run()

    needs_api = args.propose or args.full or args.verify
    api_key = None
    if needs_api:
        api_key, src = st.load_api_key(allow_command=args.allow_key_command)
        if not api_key:
            print("[错误] 未找到 DEEPSEEK_API_KEY。", file=sys.stderr)
            if "命令替换" in src:
                print(f"       {src}", file=sys.stderr)
            return 2
        print(f"[配置] key 来源：{src}（长度 {len(api_key)}，未打印内容）")
        print(f"[配置] 模型：{args.model}\n")

    if args.verify:
        return cmd_verify(args.verify, api_key, args.model, args.repeat,
                          args.margin, args.only)
    if args.propose:
        return cmd_propose(api_key, args.model)
    if args.full:
        return cmd_full(api_key, args.model, args.repeat, args.margin, args.max_iters)

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

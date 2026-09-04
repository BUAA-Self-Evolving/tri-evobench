#!/usr/bin/env python3
"""L1 benchmark 评测 runner。

流程（每个 turn）:
  1. 读 turnN/task.json -> 取 user_message（模拟用户输入）+ evaluate（gt + judge_prompt / verify.py）
  2. agent 产生回答:
     - canned 模式: 使用预置模拟回答（run_eval.py 内置，v1 平庸 / v2 改进，用于演示迭代）
     - glm 模式: 调 glm-5.3 扮演 agent，根据 user_message + workspace 文件真实作答
  3. 评测:
     - llm_as_judge: judge_prompt 填入 user_message/agent 回答/{gt} 标准答案 -> glm-5.3 打 0-1 连续分
     - rule_verifier: 调 turnN/verify.py（确定性脚本评测）
  4. 迭代: max_iterations=3, iter_score_0_threshold=0.8, 达标即停 / 无改进即停 / 取最高分

用法:
    python3 run_eval.py                  # canned agent + glm-5.3 judge
    python3 run_eval.py --agent glm      # glm-5.3 同时扮演 agent 和 judge
    python3 run_eval.py --turns turn1,turn4   # 只跑指定 turn
"""
import argparse
import copy
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
WS = BASE / "agent_workspace"
ITER_DIR = WS / "iterations"
API_URL = "http://localhost:7878/v1/messages"   # mccodex 本地代理 (anthropic-messages)
MODEL = os.environ.get("GLM_MODEL", "glm-5.3")        # judge 模型
AGENT_MODEL = os.environ.get("GLM_AGENT_MODEL", "glm-5.2")  # agent 模型
API_KEY = os.environ.get("MCCODEX_API_KEY", "")
if not API_KEY:
    try:  # 从 pi 的 models.json 读 mccodex key
        _pj = json.loads((Path.home() / ".pi/agent/models.json").read_text())
        API_KEY = _pj["providers"]["mccodex"]["apiKey"]
    except Exception:
        API_KEY = ""
THRESHOLD = 0.8
MAX_ITERS = 3


# ---------------- LLM 调用（agent 与 judge 共用 glm-5.3） ----------------
def call_glm(prompt, system=None, retries=2, model=None):
    body = {"model": model or MODEL, "max_tokens": 4000, "temperature": 0.0,
            "messages": []}
    if system:
        body["messages"].append({"role": "system", "content": system})
    body["messages"].append({"role": "user", "content": prompt})
    import time
    for i in range(retries + 1):
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {API_KEY}",
                     "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                out = json.loads(r.read())
            return "".join(c.get("text", "") for c in out.get("content", []))
        except Exception as e:
            print(f"  [glm 失败 {i+1}/{retries+1}] {e}")
            if i < retries:
                time.sleep(5)
    return None


# ---------------- agent: glm 模式（根据 user_message 真实作答） ----------------
AGENT_SYSTEM = ("你是一个严谨的项目助理 Agent。用户会提出工作请求，你根据请求与可用材料完成任务并给出最终回复。"
                "如需调用工具，在回复末尾以 JSON 行给出，格式: {\"tool_calls\": [{\"tool\": \"...\", \"arguments\": {...}}]}。"
                "回复语言跟随用户语言。")


def agent_glm(task, feedback=None):
    """用 glm-5.3 扮演 agent 处理 user_message；feedback 为上一轮评测反馈（迭代改进）。"""
    ws_desc = ""
    for pat in ("data/*.csv", "docs/*"):
        for p in sorted(WS.glob(pat)):
            ws_desc += f"\n[{p.name}]\n" + p.read_text(encoding="utf-8")[:2500]
    prompt = f"【用户请求】\n{task['user_message']}\n\n【工作空间文件】{ws_desc or '（无）'}"
    if task.get("available_tools"):
        prompt += f"\n\n【可用工具】{', '.join(task['available_tools'])}"
    if feedback:
        prompt += f"\n\n【上一轮评测反馈（请改进）】\n{feedback}"
    prompt += "\n\n请给出最终回复。"
    return call_glm(prompt, system=AGENT_SYSTEM, model=AGENT_MODEL)


# ---------------- agent: canned 模式（预置演示答案） ----------------
STATE_AFTER_T3 = {
    "facts": {"default_output_language": {"value": "en-US"},
              "aurora.technical_owner": {"value": "周凯"},
              "aurora.go_live_date": {"value": "2026-10-08"},
              "default_delivery_calendar_id": {"value": "cal_delivery_team"},
              "aurora.delivery_group_id": {"value": "grp_aurora_delivery"}},
    "fact_history": [
        {"fact_id": "F-PREF-01-V1", "key": "default_output_language", "value": "zh-CN", "status": "superseded"},
        {"fact_id": "F-WORK-02-V1", "key": "aurora.go_live_date", "value": "2026-09-30", "status": "superseded"}]}

CANNED = {
  "turn1": [
    {"response": "好的，已处理。",
     "tool_calls": [{"tool": "calendar.create_event", "arguments": {
         "calendar_id": "cal_delivery_team", "title": "Aurora 周例会",
         "start": "2026-08-24T10:00:00+08:00", "end": "2026-08-24T10:30:00+08:00"}}],
     "events": [{"event": "asked_for_calendar_id"}],
     "state": {"facts": {"default_output_language": {"value": "zh-CN"},
                         "aurora.technical_owner": {"value": "周凯"}}}},
    {"response": "已记住：默认材料用中文，Aurora 技术负责人为周凯。您平时的交付日历是 cal_delivery_team，8 月 24 日 10:00-10:30 的 Aurora 周例会已创建。"}],
  "turn2": [
    {"response": "已完成，群号已告知。", "tool_calls": [], "state": {}},
    {"response": "已查到“Aurora 交付群”系统 ID：grp_aurora_delivery，并已记住以后 Aurora 交付通知默认发这个群。风险清单已按命名规则保存为 aurora_20260820_risk.md，按高/中/低分级，每条含责任人与缓解措施。本次未发送任何消息。",
     "tool_calls": [
       {"tool": "message.resolve_group", "arguments": {"group_name": "Aurora 交付群"},
        "result": {"group_id": "grp_aurora_delivery"}},
       {"tool": "file.create_file", "arguments": {"filename": "aurora_20260820_risk.md"}}],
     "state": {"facts": {"document_filename_pattern": {"value": "{project}_{YYYYMMDD}_{doc_type}"},
                         "aurora.delivery_group_id": {"value": "grp_aurora_delivery"}}}}],
  "turn3": [
    {"response": "好的，已把日期改为 10 月 8 日。会已创建。",
     "tool_calls": [{"tool": "calendar.create_event", "arguments": {
         "calendar_id": "cal_delivery_team", "title": "Launch Readiness",
         "start": "2026-10-08T09:00:00+08:00", "end": "2026-10-08T09:30:00+08:00"}}],
     "state": STATE_AFTER_T3},
    {"response": "Confirmed: default working language is now English. Go-live is updated to 2026-10-08. The minutes aurora_20260910_launch_readiness.md and the Launch Readiness meeting (Oct 8, 09:00-09:30 on cal_delivery_team) are created. Resumen en español: el lanzamiento se pospone al 8 de octubre; quedan riesgos de integración y pruebas de regresión; el comité de preparación se reunirá esa mañana.",
     "tool_calls": [
       {"tool": "calendar.create_event", "arguments": {
           "calendar_id": "cal_delivery_team", "title": "Launch Readiness",
           "start": "2026-10-08T09:00:00+08:00", "end": "2026-10-08T09:30:00+08:00"}},
       {"tool": "file.create_file", "arguments": {"filename": "aurora_20260910_launch_readiness.md"}}],
     "state": STATE_AFTER_T3}],
  "turn4": [
    {"response": "crm-core 模块问题较多，建议加强测试。", "tool_calls": [], "events": []},
    {"response": "先确认口径：以 P0 缺陷数为主。汇总：S24 缺陷 35 个、P0 3 个；S25 缺陷 32 个、P0 3 个；S26 缺陷 23 个、P0 1 个；S27 缺陷 23 个、P0 2 个。crm-core 质量最差，P0 合计 8 个全项目最高，且覆盖率最低。覆盖率越低 CI 失败率越高，两者明显相反。",
     "events": [{"event": "asked_for_metric_definition"}]}],
  "turn5": [
    {"response": "已润色完成。", "polished_len": 900},
    {"response": "润色说明：1) 调整了首句让步从句的衔接、统一了因果表达；2) 将“Candidate rewrites are further verified”改为发布前验证的前置表达；3) 全部限定语、LaTeX 命令与专有名词原样保留。", "demo": "abstract_polished.txt"}],
  "turn6": [
    {"response": "翻译完成。", "zh_len": 200},
    {"response": "翻译完成：逐句完整译出，术语按词表（匿名化/推理/隐私–效用权衡），IntentAnony、Personal Reddit、SynthPAI 保留英文，限定语均已译出。", "demo": "abstract_zh.md"}],
  "turn7": [
    {"response": "这个技术很好，建议尽快立项。"},
    {"response": "三个要点：一，发布的文字即使删掉身份信息，也可能被大模型推理出隐私，这套系统能在发布前自动降低这种泄露风险；二，它按表达意图对文本做匿名化改写，比直接遮盖或简单改写效果更好；三，仍有残余风险，且结论仅在已测试场景下成立。是否立项投入，建议等跨模型验证报告出来再定"}]}


def prepare_workspace_files(turn, sim):
    """canned 模式下模拟 turn5/6/7 的文件产物链。"""
    docs = WS / "docs"
    orig = (docs / "abstract.txt").read_text(encoding="utf-8")
    if turn == "turn5":
        if sim.get("demo"):
            shutil.copy(BASE / "demo_good" / sim["demo"], docs / "abstract_polished.txt")
        else:
            (docs / "abstract_polished.txt").write_text(orig[:sim.get("polished_len", 900)], encoding="utf-8")
    elif turn == "turn6":
        if sim.get("demo"):
            shutil.copy(BASE / "demo_good" / sim["demo"], docs / "abstract_zh.md")
        else:
            (docs / "abstract_zh.md").write_text(
                "> 译自 Aurora 合作项目投稿摘要\n" + sim["response"][:sim.get("zh_len", 200)], encoding="utf-8")
    return {p.name: p.read_text(encoding="utf-8")[:400]
            for p in sorted(docs.glob("abstract*"))}


def extract_tool_calls(text):
    """glm agent 模式：从回复文本提取尾部 tool_calls JSON。"""
    m = re.search(r'"tool_calls"\s*:\s*(\[[\s\S]*?\])\s*\}?\s*$', text)
    if not m:
        return [], text
    try:
        calls = json.loads(m.group(1))
    except Exception:
        return [], text
    reply = text[:m.start()].strip().rstrip(",").rstrip("{").strip()
    return calls, reply


# ---------------- 评测 ----------------
def judge_score(task, sim):
    """llm_as_judge: judge_prompt + {gt} -> glm-5.3 打分。"""
    jc = task["evaluate"]["judge_config"]
    docs = WS / "docs"

    def read_doc(name):
        p = docs / name
        return p.read_text(encoding="utf-8")[:6000] if p.exists() else "(文件不存在)"

    doc_map = {
        "turn5": {"original_text": "abstract.txt", "polished_text": "abstract_polished.txt"},
        "turn6": {"source_text": "abstract_polished.txt", "translated_text": "abstract_zh.md"},
        "turn7": {"source_text": "abstract_zh.md"},
    }.get(sim["turn"], {})
    prompt = (jc["judge_prompt"]
              .replace("{user_message}", task["user_message"])
              .replace("{final_response}", sim["response"])
              .replace("{tool_calls}", json.dumps(sim.get("tool_calls", []), ensure_ascii=False))
              .replace("{final_state}", json.dumps(sim.get("state", {}), ensure_ascii=False))
              .replace("{workspace_files}", json.dumps(sim.get("workspace_files", {}), ensure_ascii=False)))
    for ph, fname in doc_map.items():
        prompt = prompt.replace("{" + ph + "}", read_doc(fname))
    gt = task["evaluate"].get("gt")
    if gt and "{gt}" in prompt:
        prompt = prompt.replace("{gt}", json.dumps(gt, ensure_ascii=False, indent=1))
    raw = call_glm(prompt)
    used_llm = raw is not None
    if not used_llm:
        return 0.0, {"error": "glm_unavailable"}, False
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group(0))
        return float(data["score"]), data, True
    except Exception:
        return 0.0, {"error": "parse_fail", "raw": raw[:300]}, True


def verifier_score(sim):
    """rule_verifier: import turnN/verify.py。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("v_mod", BASE / sim["turn"] / "verify.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    r = m.verify(sim["response"], str(WS), sim)
    return r["score"], r, True


# ---------------- 迭代循环 ----------------
def run_turn(turn, agent_mode):
    task = json.loads((BASE / turn / "task.json").read_text(encoding="utf-8"))
    etype = task["evaluate"]["type"]
    history, best = [], {"score": -1}
    for it in range(MAX_ITERS):
        # --- agent 产生回答 ---
        if agent_mode == "glm":
            feedback = best.get("detail", {}).get("rationale") if it > 0 and best["score"] < THRESHOLD else None
            if feedback is None and it > 0:
                fb_list = [c for c in best.get("detail", {}).get("checks", []) if c.get("ratio", 1) < 0.5]
                feedback = "; ".join(c.get("desc", "") for c in fb_list) or "（无具体反馈）"
            text = agent_glm(task, feedback)
            if text is None:
                history.append({"iteration": it, "engine": "agent_unavailable", "score": None,
                                "note": "agent glm 调用失败，本轮不计分"})
                continue
            calls, reply = extract_tool_calls(text)
            calls, reply = extract_tool_calls(text)
            sim = {"response": reply, "tool_calls": calls, "state": {}, "events": []}
            sim["workspace_files"] = {p.name: p.read_text(encoding="utf-8")[:400]
                                      for p in sorted((WS / "docs").glob("abstract*"))}
        else:  # canned
            sims = CANNED[turn]
            sim = copy.deepcopy(sims[min(it, len(sims) - 1)])
            sim["workspace_files"] = prepare_workspace_files(turn, sim)
        sim["turn"] = turn
        # --- 评测 ---
        if etype == "llm_as_judge":
            score, detail, ok = judge_score(task, sim)
            if not ok:
                history.append({"iteration": it, "engine": "judge_unavailable", "score": None,
                                "note": "judge glm 调用失败，本轮不计分"})
                continue
            engine = MODEL
        else:
            score, detail, _ = verifier_score(sim)
            engine = "verify.py"
        history.append({"iteration": it, "engine": engine, "score": score,
                        "user_message_head": task["user_message"][:60],
                        "response_head": sim["response"][:80]})
        if score > best["score"]:
            best = {"score": score, "iteration": it, "engine": engine, "detail": detail}
        if score >= THRESHOLD:
            break       # 达标即停
        prev = [h["score"] for h in history[:-1] if h.get("score") is not None]
        if prev and score <= prev[-1]:
            break       # 相对上一个有效分无改进即停
    return {"turn": turn, "type": etype, "passed": best["score"] >= THRESHOLD,
            "best_score": best["score"], "best_iteration": best["iteration"],
            "engine": best["engine"], "history": history}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=["canned", "glm"], default="canned",
                    help="agent 回答来源: canned=预置演示 / glm=glm-5.3 真实作答")
    ap.add_argument("--turns", default="all", help="如 turn1,turn4 或 all")
    a = ap.parse_args()
    ITER_DIR.mkdir(parents=True, exist_ok=True)
    turns = ([f"turn{i}" for i in range(1, 8)]
             if a.turns == "all" else [t.strip() for t in a.turns.split(",")])
    judge_desc = f"judge={MODEL}" if API_KEY else "MOCK(无key)"
    print(f"=== L1 评测 | user_message <- task.json | agent={a.agent}({AGENT_MODEL}) | {judge_desc} "
          f"| max_iter={MAX_ITERS} | threshold={THRESHOLD} ===\n")
    results = []
    for t in turns:
        r = run_turn(t, a.agent)
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{t}: {r['best_score']:.2f} [{mark}] engine={r['engine']} "
              f"best_iter={r['best_iteration']} iters={[h['score'] for h in r['history']]}")
    if results:
        # ---- 最终评测报告 ----
        print("\n" + "=" * 78)
        print(f"{'最终评测报告':^70}")
        print("=" * 78)
        print(f"{'Turn':<7}{'任务类型':<15}{'最终得分':<9}{'迭代次数':<9}{'达标轮':<8}{'评测引擎':<14}{'结果'}")
        print("-" * 78)
        total_iters = 0
        for r in results:
            valid = [h for h in r["history"] if h.get("score") is not None]
            n_iters = len(valid)
            total_iters += n_iters
            task = json.loads((BASE / r["turn"] / "task.json").read_text(encoding="utf-8"))
            ttype = task.get("task_type") or {"turn1": "记忆+工具", "turn2": "偏好+工具", "turn3": "修正+干扰"}[r["turn"]]
            best_it = r["best_iteration"] + 1
            mark = "PASS ✓" if r["passed"] else "FAIL ✗"
            print(f"{r['turn']:<7}{ttype:<15}{r['best_score']:<9.2f}{n_iters:<12}{best_it:<10}{r['engine']:<16}{mark}")
        print("-" * 78)
        avg = sum(r["best_score"] for r in results) / len(results)
        n_pass = sum(r["passed"] for r in results)
        print(f"平均分: {avg:.2f} | 通过: {n_pass}/{len(results)} | 总迭代次数: {total_iters} "
              f"(平均 {total_iters/len(results):.1f} 次/turn)")
    out = ITER_DIR / "eval_run.json"
    out.write_text(json.dumps({"agent": a.agent, "judge": judge_desc, "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"日志: {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""turn4 脚本评测 verifier（GAIA 风格：从 CSV 实算 gold + 答案匹配）。

设计参考：
- GAIA: 脚本化 answer matching（标准化后比对参考答案）
- τ-bench: 确定性状态/轨迹检查（不依赖 LLM）
本 turn 的 gold（sprint 汇总、最差模块）完全可由 CSV 确定性重算，无需 LLM 参与判分。

接口（被测评方调用）:
    from verify import verify
    result = verify(response, workspace, trajectory)  # -> {"score", "checks", "feedback"}

CLI:
    python verify.py --response response.txt --workspace ../agent_workspace [--trajectory traj.json]
"""
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

WEIGHTS = {
    "C1": ("给出结论前先向用户澄清口径", 0.15),
    "C2": ("sprint 汇总数值正确（8 个数字）", 0.40),
    "C3": ("模块结论正确 (crm-core) 并给出依据", 0.15),
    "C4": ("指出 coverage 与 ci_failure_rate 负相关", 0.10),
    "C5": ("结论 ≤ 5 句", 0.05),
    "C6": ("回复为中文", 0.05),
    "C7": ("无幻觉实体（不存在的 sprint/模块）", 0.10),
}
REAL_MODULES = {"auth", "api-gateway", "crm-core"}
REAL_SPRINTS = {"S24", "S25", "S26", "S27"}


def load_gold(workspace):
    """从 CSV 确定性重算标准答案。"""
    csv_path = Path(workspace) / "data" / "sprint_metrics.csv"
    sprint = defaultdict(lambda: {"bugs": 0, "p0": 0})
    module = defaultdict(lambda: {"p0": 0, "cov": [], "fail": []})
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sprint[row["sprint"]]["bugs"] += int(row["bugs_found"])
            sprint[row["sprint"]]["p0"] += int(row["p0_defects"])
            module[row["module"]]["p0"] += int(row["p0_defects"])
            module[row["module"]]["cov"].append(float(row["test_coverage_pct"]))
            module[row["module"]]["fail"].append(float(row["ci_failure_rate"]))
    worst = max(module, key=lambda m: module[m]["p0"])
    return dict(sprint), {m: dict(v) for m, v in module.items()}, worst


def check_c1(trajectory):
    return 1.0 if any("ask" in str(ev.get("event", "")).lower()
                      for ev in (trajectory or {}).get("events", [])) else 0.0


def check_c2(response, sprint_gold):
    """每个 sprint 的 bugs 合计与 P0 合计各占 1/8；数字须出现在该 sprint 名附近的窗口内。"""
    hit, miss = 0, []
    for s in ["S24", "S25", "S26", "S27"]:
        m = re.search(re.escape(s) + r"[^S]{0,120}", response)
        seg = m.group(0) if m else response
        if str(sprint_gold[s]["bugs"]) in re.findall(r"\d+", seg) or \
           re.search(rf"(?<!\d){sprint_gold[s]['bugs']}(?!\d)", seg):
            hit += 0.5
        else:
            miss.append(f"{s} bugs 合计应为 {sprint_gold[s]['bugs']}")
        if re.search(rf"(?<!\d){sprint_gold[s]['p0']}(?!\d)", seg):
            hit += 0.5
        else:
            miss.append(f"{s} P0 合计应为 {sprint_gold[s]['p0']}")
    return hit / 4.0, miss


def check_c3(response, module_gold, worst):
    ok_mod = bool(re.search(r"crm[-_ ]?core", response, re.I))
    ok_reason = any(str(module_gold[worst]["p0"]) in w for w in
                    re.findall(r"[^。；;\n]{0,60}crm[-_ ]?core[^。；;\n]{0,80}", response, re.I))
    return 1.0 if (ok_mod and ok_reason) else (0.5 if ok_mod else 0.0)


def check_c4(response):
    return 1.0 if re.search(r"(负相关|相反|越低[^\n]{0,15}越高|越高[^\n]{0,15}越低|"
                            r"覆盖[^\n]{0,10}低[^\n]{0,20}失败[^\n]{0,10}高)", response) else 0.0


def check_c5(response):
    n = len([x for x in re.split(r"[。！？.!?]", response) if x.strip()])
    return 1.0 if n <= 5 else max(0.0, 0.5 - 0.1 * (n - 5))


def check_c6(response):
    return 1.0 if re.search(r"[\u4e00-\u9fff]{5,}", response) else 0.0


def check_c7(response):
    sprints = set(re.findall(r"\bS\d{2}\b", response))
    mods = set(m for m in REAL_MODULES if re.search(re.escape(m), response, re.I))
    bad_sprints = sprints - REAL_SPRINTS
    bad_words = [w for w in ["billing", "payment", "notification", "frontend-v2"] if w in response]
    return (1.0, []) if not bad_sprints and not bad_words else \
        (0.0, [f"幻觉实体: {sorted(bad_sprints) + bad_words}"])


def verify(response, workspace, trajectory=None):
    trajectory = trajectory or {}
    sprint_gold, module_gold, worst = load_gold(workspace)
    r = {"C1": check_c1(trajectory)}
    r["C2"], fb2 = check_c2(response, sprint_gold)
    r["C3"] = check_c3(response, module_gold, worst)
    r["C4"] = check_c4(response)
    r["C5"] = check_c5(response)
    r["C6"] = check_c6(response)
    r["C7"], fb7 = check_c7(response)
    total_w = sum(WEIGHTS[k][1] for k in WEIGHTS)
    score = sum(WEIGHTS[k][1] * r[k] for k in WEIGHTS) / total_w
    checks = [{"check_id": k, "desc": WEIGHTS[k][0], "weight": round(WEIGHTS[k][1] / total_w, 4),
               "ratio": round(r[k], 4)} for k in WEIGHTS]
    feedback = fb2 + fb7 + [f"{k} 未通过: {WEIGHTS[k][0]}" for k in WEIGHTS if r[k] < 0.5]
    return {"score": round(min(1.0, max(0.0, score)), 4),
            "checks": checks, "feedback": feedback,
            "gold": {"sprint_totals": {k: v for k, v in sprint_gold.items()},
                     "module_p0": {m: v["p0"] for m, v in module_gold.items()},
                     "worst_module": worst}}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--response", help="agent 回复文本文件")
    ap.add_argument("--response-text", help="直接传回复文本")
    ap.add_argument("--workspace", default="../agent_workspace")
    ap.add_argument("--trajectory", help="JSON: {tool_calls, events, state}")
    a = ap.parse_args()
    resp = a.response_text or (Path(a.response).read_text(encoding="utf-8")
                               if a.response else "")
    traj = json.loads(Path(a.trajectory).read_text(encoding="utf-8")) if a.trajectory else {}
    print(json.dumps(verify(resp, a.workspace, traj), ensure_ascii=False, indent=2))

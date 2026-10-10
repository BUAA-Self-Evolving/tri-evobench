#!/usr/bin/env python3
"""Generate the L2 V5 capability codebook from the admitted task pool.

Every capability that has admitted pool tasks gets one entry. Domain and task
types come from the builder's capability_hint when it declares them (tau2,
AutoResearch) and from the table below otherwise. ``transfer_groups`` is the
cross-benchmark mapping table: capabilities in one group share a procedure, so
a probe drawn from one may test transfer from Turns of another.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

TAXONOMY_VERSION = "tri-evobench-l2-v5-codebook/2.0"
STATIC = {
    "sra.data_visualization": ("software_engineering", ["code_generation", "data_visualization"],
                               "Plot data with matplotlib/seaborn and return the expected Axes or figure objects."),
    "sra.dataframe_analysis": ("software_engineering", ["code_generation", "tabular_data_analysis"],
                               "Construct, transform, and summarize pandas DataFrames."),
    "sra.numerical_and_ml": ("software_engineering", ["code_generation", "numerical_computing"],
                             "Numerical computing and classical ML with numpy, scipy, scikit-learn, statistics."),
    "sra.filesystem_and_io": ("software_engineering", ["code_generation", "file_io"],
                              "Files, directories, archives, CSV/JSON, sqlite, and subprocess I/O."),
    "sra.text_processing": ("software_engineering", ["code_generation", "text_processing"],
                            "Regex, string handling, counting, and NLP text processing."),
    "sra.encoding_and_hashing": ("software_engineering", ["code_generation", "encoding"],
                                 "Binary encodings, hashing, and codecs."),
    "sra.datetime_and_time": ("software_engineering", ["code_generation", "datetime"],
                              "Date, time, and timezone computation."),
    "sra.web_and_network": ("software_engineering", ["code_generation", "web_io"],
                            "HTTP, URL, and HTML parsing code; tests run offline with mocks."),
    "sra.general_python": ("software_engineering", ["code_generation"], "Other standard-library Python tasks."),
}
# Capabilities whose builder hint is per task (AutoResearch) or whose upstream
# category does not name a domain (SkillLearnBench families) get a fixed entry.
CAPABILITY_OVERRIDES = {
    "ar.tabular_and_text_prediction": ("auto_research", ["tabular_prediction", "text_prediction"],
                                       "Classification, regression, and QA on tabular or natural-language data."),
    "ar.sequence_and_math_reasoning": ("auto_research", ["time_series_forecasting", "retrieval", "math_reasoning"],
                                       "Time-series forecasting, code retrieval, and math word problems."),
    "ar.decision_and_game_strategy": ("auto_research", ["game_strategy", "reinforcement_learning"],
                                      "Iterated-game strategies and reinforcement-learning control."),
    "ar.vision_and_deep_models": ("auto_research", ["image_classification", "molecular_property_prediction"],
                                  "Image classification and graph or molecular property models."),
    "skilllearn.court-form-filling": ("office_documents", ["form_filling"], None),
    "skilllearn.offer-letter-generator": ("office_documents", ["document_generation"], None),
    "skilllearn.stock-data-visualization": ("finance_economics", ["data_visualization"], None),
    "skilllearn.weighted-gdp-calculation": ("finance_economics", ["quantitative_analysis"], None),
    "skilllearn.dbscan-parameter-tuning": ("data_analysis", ["model_tuning"], None),
    "skilllearn.video-object-counting": ("media_production", ["media_processing"], None),
    "skilllearn.earthquake-plate-calculation": ("natural_science", ["scientific_data_analysis"], None),
}
NATIVE_DOMAIN = {
    "office-white-collar": ("office_documents", ["document_processing"]),
    "industrial-physical-systems": ("industrial_engineering", ["simulation_and_control"]),
    "finance-economics": ("finance_economics", ["quantitative_analysis"]),
    "natural-science": ("natural_science", ["scientific_data_analysis"]),
    "software-engineering": ("software_engineering", ["software_modification"]),
    "mathematics-or-formal-reasoning": ("formal_reasoning", ["planning_and_optimization"]),
    "media-content-production": ("media_production", ["media_processing"]),
    "cybersecurity": ("cybersecurity", ["security_analysis"]),
    "document-processing": ("office_documents", ["document_processing"]),
    "data-analysis": ("data_analysis", ["quantitative_analysis"]),
    "data-visualization": ("data_analysis", ["data_visualization"]),
    "machine-learning": ("data_analysis", ["model_tuning"]),
    "computer-vision": ("media_production", ["media_processing"]),
    "scientific-computing": ("natural_science", ["scientific_data_analysis"]),
}
TRANSFER_GROUPS = [
    {"group_id": "document_generation", "rationale": "Fill or generate structured office documents from a specification.",
     "capability_ids": ["skilllearn.court-form-filling", "skilllearn.offer-letter-generator", "skillsbench.office-white-collar"]},
    {"group_id": "tabular_analysis_and_reporting", "rationale": "Load tabular data, compute aggregates, and report or plot them.",
     "capability_ids": ["sra.dataframe_analysis", "sra.data_visualization", "skilllearn.weighted-gdp-calculation",
                        "skilllearn.stock-data-visualization", "skillsbench.finance-economics"]},
    {"group_id": "numerical_modeling", "rationale": "Fit, tune, or evaluate numerical and ML models on data.",
     "capability_ids": ["sra.numerical_and_ml", "skilllearn.dbscan-parameter-tuning", "skillsbench.natural-science",
                        "ar.tabular_and_text_prediction"]},
    {"group_id": "program_construction", "rationale": "Write or repair programs against a test-defined contract.",
     "capability_ids": ["sra.filesystem_and_io", "sra.text_processing", "skillsbench.software-engineering"]},
]


def native_entry(cap: str, hint: dict) -> tuple[str, list[str], str]:
    prefix, name = cap.split(".", 1)
    category = hint.get("category") or name
    domain, types = NATIVE_DOMAIN.get(category, ("general", ["file_artifact_generation"]))
    if prefix == "skilllearn":
        return domain, types, f"SkillLearnBench task family '{name}' (upstream category {category}); instances are native siblings."
    return domain, types, f"SkillsBench tasks in upstream category '{name}'."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    caps: dict[str, dict] = {}
    counts: Counter[str] = Counter()
    for path in sorted(args.pool.glob("*/*/pool_task.json")):
        record = json.loads(path.read_text())
        if not record.get("qualification", {}).get("admitted") or record.get("pool_scope") == "explicit_dependency_only":
            continue
        hint = record["capability_hint"]
        cap = hint["capability_id"]
        counts[cap] += 1
        if cap in caps:
            continue
        benchmark = record["source"]["benchmark_id"]
        if cap in STATIC:
            domain, types, description = STATIC[cap]
        elif cap in CAPABILITY_OVERRIDES:
            domain, types, description = CAPABILITY_OVERRIDES[cap]
            description = description or native_entry(cap, hint)[2]
        elif hint.get("domain_id") and hint.get("task_type_ids"):
            domain, types, description = hint["domain_id"], hint["task_type_ids"], hint.get("description") or hint.get("rationale", "")
        else:
            domain, types, description = native_entry(cap, hint)
        caps[cap] = {"capability_id": cap, "benchmark_id": benchmark, "domain_id": domain,
                     "task_type_ids": list(types), "description": description,
                     "grouping_rule": hint.get("rationale", "")}
    for cap, entry in caps.items():
        entry["admitted_pool_tasks"] = counts[cap]
    groups = []
    for group in TRANSFER_GROUPS:
        members = [c for c in group["capability_ids"] if c in caps]
        if len({caps[c]["benchmark_id"] for c in members}) >= 2:
            groups.append({**group, "capability_ids": members})
    book = {"schema_version": "l2_capability_codebook/2.0", "taxonomy_version": TAXONOMY_VERSION,
            "capabilities": [caps[k] for k in sorted(caps)], "transfer_groups": groups}
    args.out.write_text(json.dumps(book, indent=2, ensure_ascii=False) + "\n")
    by_bench = Counter(e["benchmark_id"] for e in caps.values())
    print(json.dumps({"capabilities": len(caps), "by_benchmark": by_bench, "transfer_groups": len(groups)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate the L2 V5 capability codebook from composition-eligible task records.

Admitted and recipe-ready counts remain separate. Every available capability
gets one entry. Domain and task
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

try:
    from .l2_v5_compose import composition_eligible
except ImportError:
    from l2_v5_compose import composition_eligible

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
# Individually reviewed QA capability rules; IDs remain the upstream primary skill.
STATIC.update({'sra_qa.medcalc.medcalcbench_004': ('clinical_reasoning',
                                     ['clinical_calculation'],
                                     'BMI. One upstream skill; calculate mass divided by squared '
                                     'height, converting centimetres to metres. Assign only '
                                     'instances whose sole annotation is medcalcbench_004.'),
 'sra_qa.medcalc.medcalcbench_009': ('clinical_reasoning',
                                     ['clinical_calculation'],
                                     'MAP. One upstream skill; calculate the conventional resting '
                                     'MAP estimate from systolic and diastolic pressure. Assign '
                                     'only sole annotation medcalcbench_009.'),
 'sra_qa.medcalc.medcalcbench_035': ('clinical_reasoning',
                                     ['clinical_calculation'],
                                     'Calcium correction. One upstream skill; apply the standard '
                                     'albumin correction with normal albumin explicitly fixed at 4 '
                                     'g/dL. Assign only sole annotation medcalcbench_035.'),
 'sra_qa.logic.logicbench_000': ('formal_reasoning',
                                 ['deductive_reasoning'],
                                 'Modus Ponens. One upstream skill; instantiate a conditional and '
                                 'affirm its antecedent. Negative questions ask whether the '
                                 'opposite consequent follows. Assign only sole annotation '
                                 'logicbench_000.'),
 'sra_qa.logic.logicbench_001': ('formal_reasoning',
                                 ['deductive_reasoning'],
                                 'Modus Tollens. One upstream skill; deny a consequent to infer '
                                 'the negated antecedent. Assign only sole annotation '
                                 'logicbench_001; exclude contradictory stories or probabilistic '
                                 'conditionals.'),
 'sra_qa.logic.logicbench_002': ('formal_reasoning',
                                 ['deductive_reasoning'],
                                 'Disjunctive Syllogism. One upstream skill; eliminate a false '
                                 'disjunct from an inclusive disjunction, including double '
                                 'negation. Assign only sole annotation logicbench_002; exclude '
                                 'existential-to-individual substitutions and affirming-a-disjunct '
                                 'errors.'),
 'sra_qa.theoremqa.theoremqa_292': ('mathematics',
                                    ['mathematical_reasoning'],
                                    'Definite integration and accumulated change. Exactly first '
                                    'skill theoremqa_292. Evaluate a definite integral by '
                                    'antiderivatives, symmetry, or integrating a stated rate; '
                                    'exclude rows requiring unrelated limit theory or missing '
                                    'geometry assumptions.'),
 'sra_qa.theoremqa.theoremqa_291': ('mathematics',
                                    ['mathematical_reasoning'],
                                    'Binomial counting and coefficients. Exactly first skill '
                                    'theoremqa_291. Count choices or position allocations with '
                                    'binomial coefficients, including constrained subsets and '
                                    'coefficients of a binomial power. No grouping by question '
                                    'keywords or secondary skills.'),
 'sra_qa.theoremqa.theoremqa_196': ('mathematics',
                                    ['mathematical_reasoning'],
                                    'Limits of sequences and partial sums. Exactly first skill '
                                    'theoremqa_196. Determine limits from asymptotic equivalence, '
                                    'monotonicity, weighted averaging, or partial-sum limits. '
                                    'Every row requires a limit argument; finite trivia and the '
                                    'malformed cosine row are excluded.'),
 'sra_qa.champ.champ_010': ('mathematics',
                            ['mathematical_reasoning'],
                            'Congruence and residue constraints. Exactly first skill champ_010. '
                            'Use congruence classes to constrain prime tuples, eliminate integer '
                            'solutions, prove divisibility, or evaluate powers. Secondary skills '
                            'are retained as labels but never determine assignment.'),
 'sra_qa.champ.champ_016': ('mathematics',
                            ['mathematical_reasoning'],
                            'Binomial selections and counting identities. Exactly first skill '
                            'champ_016. Translate a counting or averaging question into binomial '
                            'choices, stars and bars, or binomial identities. Prefer different '
                            'structures rather than repeated increasing-digit variants.'),
 'sra_qa.champ.champ_012': ('mathematics',
                            ['mathematical_reasoning'],
                            'Counting by disjoint cases and state recurrences. Exactly first skill '
                            'champ_012. Partition configurations by the last symbol, boundary '
                            'tile, or boundary occupancy, then add counts using a finite-state '
                            'recurrence. This is a focused algorithmic use of the upstream rule of '
                            'sum, not all additive formulas.')})

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
    "skilllearn.financial-analysis": ("finance_economics", ["financial_analysis"], None),
    "skilllearn.travel-planning": ("planning", ["travel_planning"], None),
    "skilllearn.organize-messy-files": ("software_engineering", ["file_organization"], None),
    "skilllearn.github-repo-analytics": ("software_engineering", ["repository_analysis"], None),
    "skilllearn.dependency-vulnerability-check": ("cybersecurity", ["security_analysis"], None),
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
    admitted: Counter[str] = Counter()
    recipe_ready: Counter[str] = Counter()
    for path in sorted(args.pool.glob("*/*/pool_task.json")):
        record = json.loads(path.read_text())
        if not composition_eligible(record) or record.get("pool_scope") == "explicit_dependency_only":
            continue
        hint = record["capability_hint"]
        cap = hint["capability_id"]
        counts[cap] += 1
        if record.get("qualification", {}).get("admitted"):
            admitted[cap] += 1
        else:
            recipe_ready[cap] += 1
        if cap in caps:
            continue
        benchmark = record["source"]["benchmark_id"]
        if cap in STATIC:
            domain, types, description = STATIC[cap]
        elif cap in CAPABILITY_OVERRIDES:
            domain, types, description = CAPABILITY_OVERRIDES[cap]
            description = description or native_entry(cap, hint)[2]
        elif cap.startswith("sra_qa."):
            family = cap.split(".")[1]
            domain = {"medcalc": "clinical_reasoning", "logic": "formal_reasoning"}.get(family, "mathematics")
            types = [{"medcalc": "clinical_calculation", "logic": "deductive_reasoning"}.get(family, "mathematical_reasoning")]
            description = f"Native {family} questions assigned by upstream primary skill {cap.rsplit('.', 1)[1]}."
        elif hint.get("domain_id") and hint.get("task_type_ids"):
            domain, types, description = hint["domain_id"], hint["task_type_ids"], hint.get("description") or hint.get("rationale", "")
        else:
            domain, types, description = native_entry(cap, hint)
        caps[cap] = {"capability_id": cap, "benchmark_id": benchmark, "domain_id": domain,
                     "task_type_ids": list(types), "description": description,
                     "grouping_rule": description if cap.startswith("sra_qa.") else hint.get("rationale", "")}
    for cap, entry in caps.items():
        entry["admitted_pool_tasks"] = admitted[cap]
        entry["recipe_ready_pool_tasks"] = recipe_ready[cap]
        entry["composition_eligible_pool_tasks"] = counts[cap]
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

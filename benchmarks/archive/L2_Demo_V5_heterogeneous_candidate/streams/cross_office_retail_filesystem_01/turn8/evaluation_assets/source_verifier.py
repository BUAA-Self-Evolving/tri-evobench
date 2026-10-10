import json
def verify(submission, turn_dir):
    if not isinstance(submission,str): return False,{"reason":"inline_json_required"}
    value=json.loads(submission); actual=value.get("tool_calls") if isinstance(value,dict) else None
    expected=json.loads((turn_dir/"evaluation_assets/expected_actions.json").read_text())["actions"]
    normalized=[{"name":x.get("name"),"arguments":x.get("arguments",{})} for x in actual] if isinstance(actual,list) else []
    passed=normalized==expected
    return passed,{"carrier":"tau2_expected_action_list/1.0","ordered_exact_match":passed,"expected_action_count":len(expected),"actual_action_count":len(normalized)}

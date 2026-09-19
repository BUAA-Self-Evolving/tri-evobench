import json
import pandas as pd
def task_func(result, csv_file_path="test.csv", json_file_path="test.json"):
    # Save to CSV
    df = pd.DataFrame(result)
    df.to_csv(csv_file_path, index=False)

    # Save to JSON
    with open(json_file_path, 'w') as f:
        json.dump(result, f, indent=4)

    return None

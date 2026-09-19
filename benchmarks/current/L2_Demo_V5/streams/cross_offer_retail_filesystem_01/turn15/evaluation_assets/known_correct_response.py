import collections
import json
import os
def task_func(data, json_file_name='data.json'):
    # Add new key 'a' with value 1
    data['a'] = 1

    # Calculate the frequency of values in `data`
    freq = collections.Counter(data.values())

    # Save the updated `data` and the `freq` into a JSON file
    json_data = {'data': data, 'freq': dict(freq)}
    json_file_path = os.path.join(os.getcwd(), json_file_name)
    with open(json_file_path, 'w') as json_file:
        json.dump(json_data, json_file)

    return json_file_path

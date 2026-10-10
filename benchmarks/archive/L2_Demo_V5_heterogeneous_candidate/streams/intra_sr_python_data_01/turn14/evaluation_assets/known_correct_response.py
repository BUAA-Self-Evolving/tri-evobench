from sklearn.preprocessing import StandardScaler
import numpy as np
import base64
def task_func(data):
    scaler = StandardScaler()
    standardized_data = scaler.fit_transform(data)
    standardized_data_str = np.array2string(standardized_data)
    encoded_data = base64.b64encode(standardized_data_str.encode('ascii')).decode('ascii')

    return encoded_data

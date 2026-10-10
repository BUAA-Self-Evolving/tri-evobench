import base64
import binascii
import os
import hashlib
def task_func(hex_str, salt_size):
    salt = os.urandom(salt_size)
    data = binascii.unhexlify(hex_str.replace('\\x', ''))
    salted_data = salt + data
    hash_value = hashlib.sha256(salted_data).hexdigest()

    return (base64.b64encode(salt).decode('utf-8'), hash_value)

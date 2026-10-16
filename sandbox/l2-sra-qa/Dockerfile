# Extend the existing L2 Python 3.11 image; its ID is checked by the build caller.
ARG BASE_IMAGE=trievo-l2-v5:0.7-native
FROM ${BASE_IMAGE}
RUN python -m pip install --no-cache-dir --no-deps latex2sympy2==1.9.1 antlr4-python3-runtime==4.7.2

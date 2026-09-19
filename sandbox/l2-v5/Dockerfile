FROM trievo-l2:0.1

ARG PIP_INDEX_URL=https://pypi.org/simple

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# V5 packages vendor upstream verifier code and assets, but formal execution is
# offline. Materialize the bounded verifier/tool dependency superset at build
# time so no Turn or verifier attempts a network install.
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
    'pytest==8.4.1' \
    'python-docx>=1.1,<2' \
    'openpyxl>=3.1,<4' \
    'python-pptx>=1.0,<2' \
    'pypdf>=5,<7' \
    'reportlab>=4,<5' \
    'cryptography>=43,<47' \
    'PyMuPDF>=1.24,<2' \
    'rapidfuzz>=3.9,<4' \
    'scipy>=1.13,<2'

# The base image already carries NumPy, SciPy, and Matplotlib. Pandas is the
# remaining SR-Agents dependency used by the selected V5 capabilities.
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" 'pandas>=2.2,<3'

# Some selected SR-Agents source tests import requests even though formal
# execution remains network-disabled.
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" 'requests>=2.31,<3'

WORKDIR /workspace
CMD ["sleep", "infinity"]

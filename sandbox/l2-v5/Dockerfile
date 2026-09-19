FROM trievo-l2:0.1

ARG PIP_INDEX_URL=https://pypi.org/simple

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
    'scipy>=1.13,<2'

WORKDIR /workspace
CMD ["sleep", "infinity"]

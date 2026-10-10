# Readthedocs build container with project dependencies pre-installed
FROM readthedocs/build:8.0
COPY pyproject.toml README.md LICENSE /src/
COPY requests_cache /src/requests_cache
RUN pip3 install -U /src/[docs,backends]
ENTRYPOINT ["/bin/bash"]

ARG TEST_IMAGE=repair-workbench-test:20261014-pinned
FROM ${TEST_IMAGE}
USER root
RUN apt-get update -qq && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /testbed
COPY calculator.py /testbed/calculator.py
COPY tests /testbed/tests
RUN git init -q && git add . && GIT_AUTHOR_DATE=2000-01-01T00:00:00Z \
    GIT_COMMITTER_DATE=2000-01-01T00:00:00Z git -c user.name=Fixture \
    -c user.email=fixture@localhost -c commit.gpgsign=false commit -qm baseline
CMD ["tail", "-f", "/dev/null"]

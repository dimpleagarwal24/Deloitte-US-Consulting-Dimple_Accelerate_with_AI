def pytest_addoption(parser):
    parser.addoption("--review-evidence", default=None, help="Retain offline subprocess evidence in a new directory; existing evidence is never overwritten")
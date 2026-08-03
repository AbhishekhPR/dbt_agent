class DeploymentEventAPI:
    """Small public lifecycle boundary for CI/CD callbacks and test harnesses."""

    def __init__(self, processor):
        self.processor = processor

    def post(self, payload: dict) -> dict:
        return self.processor.process(payload)

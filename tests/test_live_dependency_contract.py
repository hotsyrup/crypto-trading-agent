import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LiveDependencyContractTests(unittest.TestCase):
    def test_agentkit_solana_compatibility_is_pinned_and_smoke_checked(self) -> None:
        requirements = (ROOT / "requirements-live.txt").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("solana==0.36.10", requirements.splitlines())
        self.assertIn(
            "from coinbase_agentkit import CdpEvmWalletProvider",
            dockerfile,
        )

    def test_research_image_serves_the_trader_compatibility_contract(self) -> None:
        dockerfile = (ROOT / "Dockerfile.research").read_text(encoding="utf-8")

        self.assertIn(
            'CMD ["python", "-m", "app.research_agent"]',
            dockerfile,
        )
        self.assertNotIn("app.research_v3_app", dockerfile)


if __name__ == "__main__":
    unittest.main()

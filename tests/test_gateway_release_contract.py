"""Conservative stateless release path; never silently target the stateful worker."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GatewayReleaseContract(unittest.TestCase):
    def test_explicit_gateway_target_keeps_existing_publication_and_owner(self):
        """Both targets use the same immutable image publisher and existing GitOps action."""
        source = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('default: spi-triage', source)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.deploy_target == 'alert-graphs'", source)
        self.assertIn("github.event_name != 'workflow_dispatch' || inputs.deploy_target != 'alert-graphs'", source)
        self.assertEqual(source.count('uses: alchemmist/deploy-to-socrat@v1'), 2)
        self.assertIn('app: grafana\n          component: alertGraphs', source)
        self.assertEqual(source.count('image_version: ${{ steps.image.outputs.digest }}'), 2)
        self.assertIn('environment: spi-triage-production', source)
        self.assertNotIn('kubectl', source)
        self.assertNotIn('helm upgrade', source)

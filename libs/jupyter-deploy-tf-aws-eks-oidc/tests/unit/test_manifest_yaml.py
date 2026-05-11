import re
import unittest
from pathlib import Path
from typing import Any

import yaml
from jupyter_deploy.handlers import base_project_handler

from jupyter_deploy_tf_aws_eks_oidc.template import TEMPLATE_PATH


class TestManifest(unittest.TestCase):
    MANIFEST_PATH: Path = TEMPLATE_PATH / "manifest.yaml"
    MANIFEST: dict[str, Any] | None = None
    VARIABLES_CONFIG: dict[str, Any] | None = None
    EXPECTED_REQUIREMENTS = ["terraform", "awscli", "kubectl"]
    EXPECTED_VALUES = ["deployment_id", "open_url", "aws_region"]

    @classmethod
    def setUpClass(cls) -> None:
        with open(cls.MANIFEST_PATH) as manifest_file:
            cls.MANIFEST = yaml.safe_load(manifest_file)

        variables_config_path = TEMPLATE_PATH / "variables.yaml"
        with open(variables_config_path) as variables_config_file:
            cls.VARIABLES_CONFIG = yaml.safe_load(variables_config_file)

    def test_manifest_parses_as_yaml(self) -> None:
        self.assertIsNotNone(self.MANIFEST)

    def test_manifest_parses_as_a_dict(self) -> None:
        if self.MANIFEST is None:
            self.fail("MANIFEST is None")
        self.assertIsInstance(self.MANIFEST, dict)

    def test_manifest_parsable_by_jd(self) -> None:
        manifest = base_project_handler.retrieve_project_manifest(self.MANIFEST_PATH)
        self.assertIsNotNone(manifest)

    def test_all_expected_requirements_declared(self) -> None:
        if self.MANIFEST is None:
            self.fail("MANIFEST is None")

        requirements = self.MANIFEST.get("requirements", [])
        requirement_names = [req.get("name") for req in requirements]

        for expected_req in self.EXPECTED_REQUIREMENTS:
            self.assertIn(expected_req, requirement_names, f"Expected requirement {expected_req} missing from manifest")

    def test_all_expected_values_declared(self) -> None:
        if self.MANIFEST is None:
            self.fail("MANIFEST is None")

        values = self.MANIFEST.get("values", [])
        value_names = [val.get("name") for val in values]

        for expected_val in self.EXPECTED_VALUES:
            self.assertIn(expected_val, value_names, f"Expected value {expected_val} missing from manifest")

    def test_output_sourced_values_have_matching_terraform_outputs(self) -> None:
        if self.MANIFEST is None:
            self.fail("MANIFEST is None")

        outputs_tf = (TEMPLATE_PATH / "engine" / "outputs.tf").read_text()
        tf_output_names = set(re.findall(r'^output "(\w+)"', outputs_tf, re.MULTILINE))

        for value in self.MANIFEST.get("values", []):
            if value.get("source") != "output":
                continue
            source_key = value["source-key"]
            self.assertIn(
                source_key,
                tf_output_names,
                f"Manifest value '{value['name']}' references output '{source_key}' not found in outputs.tf",
            )

    def test_project_store_type_is_defined(self) -> None:
        if self.MANIFEST is None:
            self.fail("MANIFEST is None")

        project_store = self.MANIFEST.get("project-store")
        self.assertIsNotNone(project_store, "Manifest must define 'project-store'")
        self.assertIn("store-type", project_store, "project-store must define 'store-type'")
        self.assertIn(
            project_store["store-type"],
            ["s3-only", "s3-ddb"],
            f"Unexpected store-type: {project_store['store-type']}",
        )

    def test_secrets_names_map_to_required_sensitive_variables(self) -> None:
        if self.MANIFEST is None or self.VARIABLES_CONFIG is None:
            self.fail("MANIFEST or VARIABLES_CONFIG is None")

        required_sensitive = set(self.VARIABLES_CONFIG.get("required_sensitive", {}).keys())

        for secret in self.MANIFEST.get("secrets", []):
            self.assertIn(
                secret["name"],
                required_sensitive,
                f"Manifest secret '{secret['name']}' not found in variables.yaml required_sensitive",
            )

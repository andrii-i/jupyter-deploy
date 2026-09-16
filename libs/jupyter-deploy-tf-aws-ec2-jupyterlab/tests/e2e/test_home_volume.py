"""E2E tests for file-system operations on the Jupyter home volume.

Ported from the base template with the authorization step dropped: there is no GitHub user to
allowlist, so ``ensure_server_running()`` is the only prerequisite.

Deliberately elsewhere:
  - ``test_home_volume_persists_across_host_restart`` lives in ``test_host.py``, which owns the
    suite's single EC2 stop/start cycle. Asserting persistence here would buy a second ~4-minute
    cycle for one `stat`.
"""

from pathlib import Path

from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.files import (
    upload_file_on_server,
    verify_dir_exists_on_server,
    verify_file_exists_on_server,
    verify_file_or_dir_does_not_exist_on_server,
)


def test_fs_operations(e2e_deployment: EndToEndDeployment) -> None:
    """Read, write, execute and nested-directory operations all work on the home volume.

    The home volume is an EBS volume mounted over ``/home/jovyan`` at runtime, so ordinary
    file-system semantics are not a given: a wrong mount, ownership or option would show up as a
    permission error on a plain `touch` or a script that will not execute.
    """
    e2e_deployment.ensure_server_running()

    # A file uploaded from the test machine must be executable on the volume.
    script_path_home = "test_script.sh"
    test_script = Path(__file__).parent / "files" / "test_script.sh"
    upload_file_on_server(e2e_deployment, test_script, script_path_home)
    verify_file_exists_on_server(e2e_deployment, script_path_home)

    result = e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "sh", script_path_home])
    assert "Script executed successfully" in result.stdout, "Expected script to execute successfully on home volume"

    # And a data file must round-trip byte-for-byte.
    data_file_home = "e2e_test_data.txt"
    test_data = Path(__file__).parent / "files" / "data_sample.txt"
    upload_file_on_server(e2e_deployment, test_data, data_file_home)
    verify_file_exists_on_server(e2e_deployment, data_file_home)

    result = e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "cat", data_file_home])
    assert "This is test data for E2E testing" in result.stdout, "Expected file content to match"

    # Nested directories: the depth is the point — a shallow mkdir can succeed where a deep one
    # fails on a misconfigured mount.
    test_top_dir_home = "e2e_workspace"
    test_dir_home = f"{test_top_dir_home}/subdir1/nested1"
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "mkdir", "-p", test_dir_home])
    verify_dir_exists_on_server(e2e_deployment, test_dir_home)

    test_dir_home_2 = f"{test_top_dir_home}/subdir2/nested2/deep2"
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "mkdir", "-p", test_dir_home_2])
    verify_dir_exists_on_server(e2e_deployment, test_dir_home_2)

    test_file_in_dir = f"{test_dir_home}/test_file.txt"
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "touch", test_file_in_dir])
    verify_file_exists_on_server(e2e_deployment, test_file_in_dir)

    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "rm", "-f", test_file_in_dir])
    verify_file_or_dir_does_not_exist_on_server(e2e_deployment, test_file_in_dir)

    e2e_deployment.cli.run_command(
        ["jupyter-deploy", "server", "exec", "--", "rm", "-rf", script_path_home, data_file_home, test_top_dir_home]
    )

    verify_file_or_dir_does_not_exist_on_server(e2e_deployment, script_path_home)
    verify_file_or_dir_does_not_exist_on_server(e2e_deployment, data_file_home)
    verify_file_or_dir_does_not_exist_on_server(e2e_deployment, test_top_dir_home)

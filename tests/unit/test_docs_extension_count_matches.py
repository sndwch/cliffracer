import os
import re


def test_readme_extension_table_matches_packages_dir():
    """Assert that the README table lists exactly the extensions that exist in packages/"""

    packages_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "packages"
    )
    actual_packages = [d for d in os.listdir(packages_dir) if d.startswith("cliffracer-")]

    readme_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "README.md"
    )
    with open(readme_path) as f:
        readme_content = f.read()

    # Find all table rows matching | `cliffracer-xxx` |
    table_packages = re.findall(r"\|\s*`(cliffracer-[^`]+)`\s*\|", readme_content)

    # We only care about the main table, so we grab unique packages listed in that format
    # The README might mention them multiple times, but the table format | `name` | is distinct.
    unique_table_packages = list(set(table_packages))

    missing_in_readme = set(actual_packages) - set(unique_table_packages)
    missing_in_dir = set(unique_table_packages) - set(actual_packages)

    assert not missing_in_readme, (
        f"Packages exist in dir but are missing from README table: {missing_in_readme}"
    )
    assert not missing_in_dir, (
        f"Packages listed in README table but missing from dir: {missing_in_dir}"
    )
    assert len(actual_packages) == len(unique_table_packages), (
        f"Expected {len(actual_packages)} packages in README, found {len(unique_table_packages)}"
    )

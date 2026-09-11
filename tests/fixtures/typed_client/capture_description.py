"""Write the fixture service Description to description.json."""

from pathlib import Path

from cliffracer.introspect import canonical, describe

from .service import Warehouse

SERVICE = "warehouse_e2e"
VERSION = "3.1.4"
PATH = Path(__file__).with_name("description.json")


def current() -> str:
    return canonical(describe(Warehouse, service=SERVICE, version=VERSION).to_dict())


def main() -> None:
    PATH.write_text(current() + "\n")
    print(f"wrote {PATH}")


if __name__ == "__main__":
    main()

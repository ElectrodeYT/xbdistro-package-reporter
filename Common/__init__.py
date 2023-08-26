import yaml
from dataclasses import dataclass


@dataclass
class Rules:
    class InvalidRuleException(Exception):
        # Thrown when there was an error parsing a rule
        pass

    def __init__(self, file: str):
        self.rules = None
        # Check if there is a rules file
        try:
            with open(file, "r") as file:
                try:
                    print("Loading rules file '{}'".format(file))
                    self.rules = yaml.load(file, Loader=yaml.SafeLoader)
                except yaml.YAMLError as exc:
                    print("Got YAML error reading rules file '{}':".format(file), exc)
        except FileNotFoundError:
            print("Rule file '{}' not found!".format(file))
            pass

    def translatePackage(self, name: str) -> str | None:
        if self.rules is not None and name in self.rules:
            # Parse rule
            if "action" in self.rules[name]:
                action = self.rules[name]["action"]
                if action == "alias":
                    if "alias" in self.rules[name]:
                        return self.rules[name]["alias"]
                    else:
                        raise self.InvalidRuleException
                elif action == "ignore":
                    return None
            raise self.InvalidRuleException
        else:
            return name


@dataclass
class ForeignPackage:
    package: str
    version: str
    update_status: str


@dataclass
class DistroPackage:
    package: str
    version: str
    upstream_version: str
    upstream_repo: str

    found_upstream: bool

    file: str
    line: str

    def toJSON(self):
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)

    def getPackageUpstreamFailedReason(self) -> str:
        return self.upstream_version

    def is_local_rolling(self) -> bool:
        return "ROLLING_ID" in self.version

    def is_upstream_rolling(self) -> bool:
        return "Rolling" in self.upstream_version


@dataclass
class UpstreamRequest:
    upstream_version: str
    newest_repo: str

    found: bool


@dataclass
class ForeignRepositoryChangeReport:
    packages: [ForeignPackage]

    def __init__(self):
        self.packages = []


@dataclass
class ForeignRepository:
    canonical_repo_name: str = ""

    def __init__(self):
        self.change_report = ForeignRepositoryChangeReport()
        self.rules = None
        # Check if there is a rules file
        try:
            with open("rules/{}.yml".format(self.canonical_repo_name), "r") as file:
                try:
                    print("Loading rules file for {}".format(self.canonical_repo_name))
                    self.rules = yaml.load(file, Loader=yaml.SafeLoader)
                except yaml.YAMLError as exc:
                    print("Got YAML error reading rules file for {}:".format(self.canonical_repo_name), exc)
        except FileNotFoundError:
            print("No rules file for {}!".format(self.canonical_repo_name))
            pass

    def get_change_report(self):
        return self.change_report

    def get_local_package_version(self, name: str) -> str | None:
        return None

    def get_package_version(self, name: str) -> str | None:
        if self.rules is not None and name in self.rules:
            # Parse rule
            if "action" in self.rules[name]:
                action = self.rules[name]["action"]
                if action == "alias":
                    if "alias" in self.rules[name]:
                        return self.get_local_package_version(self.rules[name]["alias"])
                    else:
                        raise self.InvalidRuleException
                elif action == "ignore":
                    return None
            raise self.InvalidRuleException
        else:
            return self.get_local_package_version(name)

    def get_repo_name(self) -> str:
        return ""
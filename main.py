import copy
import os
from git import Repo
from XBStrapDistro import XBStrapDistro
from pprint import pprint

import libversion
from dataclasses import dataclass

# Needed for various things
import yaml

# Needed for nix-os
import json
import brotli
import urllib.request

# SQLite3
import sqlite3
import base64
from contextlib import closing
# Not used since it creates a bunch of data we dont need for this
# from XBStrapSQLite import XBStrapSQLite, XBStrapSQLiteChangeReport

# PDF
from fpdf import FPDF, XPos, YPos
import time
from datetime import date

### SETTINGS
### Change these things.
# URL to the bootstrap repository of the xbstrap distribution.
repo_url = "https://github.com/managarm/bootstrap-managarm.git"
# Directory to clone into
repo_dir = "boostrap-managarm"
# Name of distribution
distro_name = "Managarm"


### CODE
distro = XBStrapDistro(repo_dir)
nix_os_repo = "https://channels.nixos.org/nixos-{}/packages.json.br"


ignored_packages: [str] = []


class ReportPDF(FPDF):
    def header(self):
        self.set_font("helvetica", "B", 15)
        self.cell(90, 10, "{} Package Report".format(distro_name), 1, new_x=XPos.RIGHT, new_y=YPos.TOP, align="C")
        self.ln(20)

    def footer(self) -> None:
        # Position at 1.5 cm from bottom
        self.set_y(-15)
        # Arial italic 8
        self.set_font('helvetica', 'I', 8)
        # Page number
        self.cell(0, 10, 'Page ' + str(self.page_no()) + '/{nb}', 0, new_x=XPos.RIGHT, new_y=YPos.TOP, align='C')


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

    def toJSON(self):
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)

    def getPackageUpstreamFailedReason(self) -> str:
        return self.upstream_version


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


class NixOSRepository(ForeignRepository):
    def __do_package_sql(self, c: sqlite3.Cursor, package: (str, str)) -> str:
        # Check if the package is known
        c.execute("SELECT EXISTS (SELECT 1 FROM nix_os_{} WHERE package='{}')".format(self.branch, package[0]))
        if not c.fetchone()[0]:
            # Package not known, add and return "new"
            c.execute("INSERT INTO nix_os_{}(package, version) VALUES('{}', '{}')".format(self.branch, package[0],
                                                                                          package[1]))
            return "new"
        else:
            # Check if we know the correct version already
            c.execute("SELECT version FROM nix_os_{} WHERE package='{}'".format(self.branch, package[0]))
            if c.fetchone()[0] == package[1]:
                return ""
            # Its different, update it
            c.execute(
                "UPDATE nix_os_{} SET version='{}' WHERE package='{}'".format(self.branch, package[1], package[0]))
            return "updated"

    def get_repo_name(self) -> str:
        return "nix-os-{}".format(self.branch)

    def get_local_package_version(self, name: str) -> str | None:
        if name in self.package_json["packages"]:
            return self.package_json["packages"][name]["version"]
        elif name in self.pname_translation:
            return self.package_json["packages"][self.pname_translation[name]]["version"]
        else:
            return None

    def __init__(self, c: sqlite3.Cursor, branch: str):
        self.canonical_repo_name = "nix-os-{}".format(branch)
        super().__init__()
        global nix_os_repo
        self.branch = branch
        self.url = nix_os_repo.format(branch)
        self.package_brotli_file = "packages-nixos-{}.json.br".format(branch)
        # Ensure the SQL table exists
        c.execute("CREATE TABLE IF NOT EXISTS nix_os_{}(package CHAR PRIMARY KEY, version CHAR)".format(branch))

        print("Importing NixOS repository, branch {}".format(branch))
        urllib.request.urlretrieve(self.url, self.package_brotli_file)
        with open(self.package_brotli_file, "rb") as file:
            self.package_raw_json = brotli.decompress(file.read())
        self.package_json = json.loads(self.package_raw_json)

        packages = []
        self.pname_translation = {}
        print("Getting package pnames")
        for package in self.package_json["packages"]:
            packages.append((package, self.package_json["packages"][package]["version"]))
            self.pname_translation[self.package_json["packages"][package]["pname"]] = package

        print("Doing SQL stuff")
        for package in packages:
            report = self.__do_package_sql(c, package)
            if report != "":
                package_report = ForeignPackage
                package_report.package = package[0]
                package_report.version = package[1]
                package_report.update_status = report
                self.change_report.packages.append(package_report)
        pprint(self.change_report)


@dataclass
class DistroPackageStatus:
    packages: [DistroPackage]

    def __init__(self, xb_distro: XBStrapDistro | None):
        self.packages = []

        if xb_distro is None:
            return

        global ignored_packages
        for xb_package in xb_distro.packages:
            # If this package is supposed to be ignored, then do so
            if xb_package.name in ignored_packages:
                continue

            # Get upstream version and repo, and fill with blank if not found
            found_upstream: bool = False
            upstream_version: str = ""
            upstream_repo: str = ""

            # If this is a rolling version package, then just fail, as we cant accurately compare the version here
            if "ROLLING" in xb_package.source.version:
                upstream_version = "Rolling version"
            else:
                upstream_result: UpstreamRequest = get_most_up_to_date_upstream_package(xb_package.name)
                if upstream_result.found:
                    upstream_version = upstream_result.upstream_version
                    upstream_repo = upstream_result.newest_repo
                    found_upstream = True
                else:
                    upstream_version = "Not found in repository (different name?)"

            package: DistroPackage = DistroPackage(package=xb_package.name,
                                                   version=xb_package.source.version,
                                                   upstream_version=upstream_version,
                                                   upstream_repo=upstream_repo,
                                                   found_upstream=found_upstream)
            self.packages.append(package)

    def toJSON(self):
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)

    @staticmethod
    def fromJSON(json_data: str):
        ret: DistroPackageStatus = DistroPackageStatus(None)
        dct = json.loads(json_data)
        if "packages" not in dct:
            raise ValueError("JSON does not contain a 'packages' entry")
        for package in dct["packages"]:
            if not ("package" in package and
                    "version" in package and
                    "upstream_version" in package
                    and "upstream_repo" in package
                    and "found_upstream" in package):
                raise ValueError("Package '{}' is missing required entries")
            package: DistroPackage = DistroPackage(package=package["package"],
                                                   version=package["version"],
                                                   upstream_version=package["upstream_version"],
                                                   upstream_repo=package["upstream_repo"],
                                                   found_upstream=package["found_upstream"])
            ret.packages.append(package)
        return ret

    def countOutOfDate(self):
        result: int = 0
        for package in self.packages:
            if libversion.version_compare2(package.version, package.upstream_version) < 0:
                result += 1
        return result

    def getPackage(self, name: str) -> DistroPackage | None:
        for package in self.packages:
            if package.package == name:
                return package
        return None

    def getOutOfDatePackages(self) -> [DistroPackage]:
        ret: [DistroPackage] = []
        for package in self.packages:
            if libversion.version_compare2(package.upstream_version, package.version) > 0:
                ret.append(package)
        return ret


@dataclass
class DistroPackageStatusDiff:
    new_packages: []
    locally_updated_packages: []
    upstream_updated_packages: []
    newly_out_of_date_packages: []

    def __init__(self, current: DistroPackageStatus, old: DistroPackageStatus):
        self.new_packages = []
        self.locally_updated_packages = []
        self.upstream_updated_packages = []
        self.newly_out_of_date_packages = []

        # We want to check if a package is new, has been removed, has gotten in date, or has gone further out of date
        # First, we iterate over the current set of packages
        for package in current.packages:
            # Get this package in old
            old_package: DistroPackage | None = None
            for old_package_search in old.packages:
                if old_package_search.package == package.package:
                    old_package = old_package_search
                    break
            if old_package is None:
                self.new_packages.append(package.package)
                continue

            # Check if this package has been updated locally
            if libversion.version_compare2(package.version, old_package.version) > 0:
                self.locally_updated_packages.append(package.package)

            # Check if the upstream version has been updated
            if libversion.version_compare2(package.upstream_version, old_package.upstream_version) > 0:
                self.upstream_updated_packages.append(package.package)

            # Check if a package that was in date has gotten out of date
            if libversion.version_compare2(package.upstream_version,
                                           package.version) > 0 and libversion.version_compare2(
                    old_package.upstream_version, old_package.version) <= 0:
                self.newly_out_of_date_packages.append(package.package)


foreign_repositories: [ForeignRepository] = []
# Name, Upstream Version
packages_out_of_date: {str} = {}


def get_most_up_to_date_upstream_package(name: str) -> UpstreamRequest:
    result: UpstreamRequest = UpstreamRequest(upstream_version="", newest_repo="", found=False)
    for repo in foreign_repositories:
        upstream_version = repo.get_package_version(name)
        if result.found == False and upstream_version is not None:
            result = UpstreamRequest(upstream_version=upstream_version, newest_repo=repo.get_repo_name(), found=True)
        elif upstream_version is not None:
            if libversion.version_compare2(upstream_version, result.upstream_version) > 0:
                result = UpstreamRequest(upstream_version=upstream_version, newest_repo=repo.get_repo_name(),
                                         found=True)
    return result


def update_git_repo():
    if os.path.exists(os.path.join(repo_dir, ".git")):
        repo = Repo(repo_dir)
        assert not repo.bare
        origin = repo.remotes.origin
        origin.pull()
    else:
        repo = Repo.clone_from(repo_url, repo_dir)
        assert not repo.bare


def perform_init():
    print("Initializing git repository")
    update_git_repo()
    print("Reading global sources")
    distro.import_global_sources("bootstrap.yml")
    print("Reading packages")
    distro.import_packages("bootstrap.yml")


def pdf_add_new_page(pdf: ReportPDF, heading: str | None = None):
    pdf.add_page()
    if heading is not None:
        pdf.set_font("helvetica", size=20)
        pdf.cell(0, 15, heading, 0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", size=12)


def print_report_pdf(current_distro_status: DistroPackageStatus, diff: DistroPackageStatusDiff | None, last_checks: []):
    filename = date.today().strftime("report-%d-%m-%Y-%I-%M-%f.pdf")
    pdf = ReportPDF()
    pdf.alias_nb_pages()

    # Print Title Page
    pdf.add_page()
    pdf.set_font("helvetica", size=24)
    pdf.cell(0, 15, "{} Package Report".format(distro_name), 0, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.set_font("helvetica", size=20)
    pdf.cell(0, 15, "Summary", 0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", size=12)
    pdf.cell(0, 10, "Total amount of known packages: {}".format(len(current_distro_status.packages)), 1,
             new_x=XPos.LMARGIN,
             new_y=YPos.NEXT)
    pdf.cell(0, 10, "Total amount of out of date packages: {}".format(current_distro_status.countOutOfDate()), 1,
             new_x=XPos.LMARGIN,
             new_y=YPos.NEXT)
    if last_checks is not None and len(last_checks):
        with pdf.table() as table:
            index_row = table.row()
            index_row.cell("Last check")
            index_row.cell("Amount Out of Date")
            index_row.cell("Amount of packages")
            for check in last_checks:
                row = table.row()
                for cell in check:
                    row.cell(str(cell))

    # Print page for packages which have gotten out of date
    if len(diff.newly_out_of_date_packages):
        pdf_add_new_page(pdf, "Newly out of date")
        pdf.cell(0, 10, "The following packages have gotten out of date:", 0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        with pdf.table() as table:
            index_row = table.row()
            index_row.cell("Package Name")
            index_row.cell("Local Version")
            index_row.cell("Upstream Version")
            for name in diff.newly_out_of_date_packages:
                row = table.row()
                row.cell(name)
                package = current_distro_status.getPackage(name)
                if package is None:
                    row.cell("Error getting package information: getPackage() returned None")
                else:
                    row.cell(package.version)
                    row.cell(package.upstream_version)

    if len(diff.upstream_updated_packages):
        pdf_add_new_page(pdf, "Updated upstream")
        pdf.cell(0, 10, "The following packages have been updated upstream:", 0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        with pdf.table() as table:
            index_row = table.row()
            index_row.cell("Package Name")
            index_row.cell("Local Version")
            index_row.cell("Upstream Version")
            for name in diff.upstream_updated_packages:
                row = table.row()
                row.cell(name)
                package = current_distro_status.getPackage(name)
                if package is None:
                    row.cell("Error getting package information: getPackage() returned None")
                else:
                    row.cell(package.version)
                    row.cell(package.upstream_version)

    if len(diff.locally_updated_packages):
        pdf_add_new_page(pdf, "Updated locally")
        pdf.cell(0, 10, "The following packages have been updated locally:", 0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        with pdf.table() as table:
            index_row = table.row()
            index_row.cell("Package Name")
            index_row.cell("Local Version")
            index_row.cell("Upstream Version")
            for name in diff.locally_updated_packages:
                row = table.row()
                row.cell(name)
                package = current_distro_status.getPackage(name)
                if package is None:
                    row.cell("Error getting package information: getPackage() returned None")
                else:
                    row.cell(package.version)
                    row.cell(package.upstream_version)

    if len(diff.new_packages):
        pdf_add_new_page(pdf, "New packages")
        pdf.cell(0, 10, "The following packages have been added:", 0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        with pdf.table() as table:
            index_row = table.row()
            index_row.cell("Package Name")
            index_row.cell("Local Version")
            index_row.cell("Upstream Version")
            for name in diff.new_packages:
                row = table.row()
                row.cell(name)
                package = current_distro_status.getPackage(name)
                if package is None:
                    row.cell("Error getting package information: getPackage() returned None")
                else:
                    row.cell(package.version)
                    row.cell(package.upstream_version)

    out_of_date_packages = current_distro_status.getOutOfDatePackages()
    if len(out_of_date_packages):
        pdf_add_new_page(pdf, "Out of date packages")
        pdf.cell(0, 10, "In total, the following packages are out of date:", 0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        with pdf.table() as table:
            index_row = table.row()
            index_row.cell("Package Name")
            index_row.cell("Local Version")
            index_row.cell("Upstream Version")
            for package in out_of_date_packages:
                row = table.row()
                row.cell(package.package)
                row.cell(package.version)
                row.cell(package.upstream_version)

    pdf.output(filename)


def main():
    database = sqlite3.connect("packages.db")
    perform_init()

    print("Reading foreign repositories")
    foreign_repositories.append(NixOSRepository(database.cursor(), "unstable"))

    print("Creating current distro status")
    current_distro_status: DistroPackageStatus = DistroPackageStatus(distro)
    # pprint(current_distro_status)
    # print(current_distro_status.toJSON())

    # Check if we have the previous one (aka check if we have run before)
    # If we do not, treat the current status as the diff
    diff: DistroPackageStatusDiff | None = None
    with closing(database.cursor()) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS previous_check_json(unix_timestamp INT PRIMARY KEY, json_distro_state_b64 CHAR)")
        c.execute("SELECT json_distro_state_b64 FROM previous_check_json ORDER BY unix_timestamp DESC LIMIT 1")
        base64_encoded = c.fetchone()
        if base64_encoded is not None:
            previous_check = DistroPackageStatus.fromJSON(base64.b64decode(base64_encoded[0]))
            diff = DistroPackageStatusDiff(current_distro_status, previous_check)

    pprint(diff)
    with closing(database.cursor()) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS check_metadata(last_check CHAR, amount_ood INT, amount INT, unix_timestamp INT PRIMARY KEY)")
        c.execute("SELECT last_check, amount_ood, amount FROM check_metadata ORDER BY unix_timestamp DESC LIMIT 5")
        print_report_pdf(current_distro_status, diff, c.fetchall())

    with closing(database.cursor()) as c:
        timestamp: int = int(time.time())
        c.execute("INSERT INTO check_metadata(last_check, amount_ood, amount, unix_timestamp) VALUES "
                  "('{}', {}, {}, {})".format(date.today().strftime("%d/%m/%Y"),
                                              current_distro_status.countOutOfDate(),
                                              len(current_distro_status.packages),
                                              timestamp))

        # Add this check into the check history table
        base64_encoded: str = base64.b64encode(current_distro_status.toJSON().encode()).decode()
        # print(base64_encoded)
        c.execute("INSERT INTO previous_check_json(unix_timestamp, json_distro_state_b64) VALUES('{}', '{}')".format(
            timestamp,
            base64_encoded))

    database.commit()


if __name__ == '__main__':
    main()

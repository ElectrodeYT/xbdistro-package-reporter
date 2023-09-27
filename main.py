import copy
import os

import XBStrapSQLite
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

# Email
import email, smtplib, ssl
from email import encoders
from email.utils import parseaddr
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Local things
from Common import *

### SETTINGS
### Change these things.
# URL to the bootstrap repository of the xbstrap distribution.
repo_url: str = "https://github.com/managarm/bootstrap-managarm.git"
# Directory to clone into
repo_dir: str = "bootstrap-managarm"
# Name of distribution
distro_name: str = "Managarm"
# Maintain a complete SQLite3 database of the distro.
# Used for some more advanced API calls in the Flask server.
maintain_xbdistro_sqllite_database: bool = True


## Send emails
send_emails: bool = False
# Send the generic report email to everyone listed in the sql database
send_generic_email: bool = False
# Send an email to a maintainer whenever a package has gotten out of date
send_maintainer_email: bool = False
# Message unsubscribe contact
# Must be filled with instructions (such as a web link or an email contact) on how to unsubscribe from the generic
# email mailing list
# Recommended is to use the Flask web-server.py with its email code to produce an unsubscribe email.
message_unsubscribe_contact: str = ""
# If true, replace {} message_unsubscribe_contact with the destination email address
message_unsubscribe_contact_fill_in_email: bool = False
# Fallback email address for packages without a maintainer
# If empty, drop it
no_maintainer_fallback_email = ""

# SMTP Host settings
smtp_host: str = "localhost"
smtp_port: int = 1025
# Server uses TLS.
smtp_is_secure: bool = False
# Server supports AUTH extension, aka logins.
# You probably want to set this to true.
smtp_do_auth: bool = False
# e-mail address.
smtp_email_address: str = ""
# User login and password.
smtp_login_user: str = smtp_email_address
smtp_login_password: str = ""

### CODE
distro = XBStrapDistro(repo_dir)
nix_os_repo = "https://channels.nixos.org/nixos-{}/packages.json.br"

ssl_context = ssl.create_default_context()

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
                                                   found_upstream=found_upstream,
                                                   file=xb_package.file,
                                                   line=xb_package.line)
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
                    "upstream_version" in package and
                    "upstream_repo" in package and
                    "found_upstream" in package and
                    "file" in package and
                    "line" in package):
                raise ValueError("Package '{}' is missing required entries")
            package: DistroPackage = DistroPackage(package=package["package"],
                                                   version=package["version"],
                                                   upstream_version=package["upstream_version"],
                                                   upstream_repo=package["upstream_repo"],
                                                   found_upstream=package["found_upstream"],
                                                   file=package["file"],
                                                   line=package["line"])
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


def perform_db_init(c: sqlite3.Cursor):
    c.execute(
        "CREATE TABLE IF NOT EXISTS previous_check_json(unix_timestamp INT PRIMARY KEY, json_distro_state_b64 CHAR)")
    c.execute(
        "CREATE TABLE IF NOT EXISTS check_metadata(last_check CHAR, amount_ood INT, amount INT, unix_timestamp INT PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS generic_email_recipients(email CHAR PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS generic_email_unsubscribe_key(email CHAR PRIMARY KEY, code CHAR)")
    c.execute("CREATE TABLE IF NOT EXISTS generic_email_subscribe_key(code CHAR PRIMARY KEY, email CHAR)")

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

    if diff is not None:
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

    # Generate a list of maintainerless and newer-than-upstream packages and print it as well
    maintainerless_packages: [DistroPackage] = []
    newer_than_upstream: [DistroPackage] = []
    for distro_package in distro.packages:
        package = current_distro_status.getPackage(distro_package.name)
        if package is None:
            continue
        if distro_package.metadata.maintainer is None or not distro_package.metadata.maintainer:
            maintainerless_packages.append(package)
        if package.is_local_rolling() or package.is_upstream_rolling() or not package.found_upstream:
            continue
        if package.upstream_version and libversion.version_compare2(package.version, package.upstream_version) > 0:
            newer_than_upstream.append(package)

    if len(maintainerless_packages):
        pdf_add_new_page(pdf, "Packages without maintainers")
        pdf.cell(0, 10, "In total, the following packages have no defined maintainer:", 0, new_x=XPos.LMARGIN,
                 new_y=YPos.NEXT)
        with pdf.table() as table:
            index_row = table.row()
            index_row.cell("Package Name")
            index_row.cell("Local Version")
            index_row.cell("Upstream Version")
            for package in maintainerless_packages:
                row = table.row()
                row.cell(package.package)
                row.cell(package.version)
                row.cell(package.upstream_version)

    if len(newer_than_upstream):
        pdf_add_new_page(pdf, "Packages newer than known upstream versions")
        pdf.cell(0, 10, "In total, the following packages are newer than any known upstream version:", 0,
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        with pdf.table() as table:
            index_row = table.row()
            index_row.cell("Package Name")
            index_row.cell("Local Version")
            index_row.cell("Upstream Version")
            for package in newer_than_upstream:
                row = table.row()
                row.cell(package.package)
                row.cell(package.version)
                row.cell(package.upstream_version)

    pdf.output(filename)
    # Symlink the file to "latest-report.pdf"
    if os.path.exists("latest-report.pdf"):
        os.remove("latest-report.pdf")
    os.symlink(filename, "latest-report.pdf")


# Generate the generic report email.
def generate_report_email(report_file: MIMEBase, recipient: str) -> MIMEMultipart:
    message = MIMEMultipart()
    message["From"] = smtp_email_address
    message["Subject"] = date.today().strftime("{} package report for %d/%m/%Y".format(distro_name))
    message["To"] = recipient
    message["Bcc"] = recipient

    # Generate message body
    body = "The latest package report for {} has been generated.\n" \
        .format(distro_name)
    if message_unsubscribe_contact:
        if message_unsubscribe_contact_fill_in_email:
            body += message_unsubscribe_contact.format(recipient)
        else:
            body += message_unsubscribe_contact
    message.attach(MIMEText(body, "plain"))

    # Attach PDF file
    message.attach(report_file)

    return message


# Generate the maintainer email.
# We call this function for each maintainer, as a decent amount of stuff is randomized.
def generate_maintainer_email(report_file: MIMEBase, package_list: [str]) -> MIMEMultipart:
    message = MIMEMultipart()
    message["From"] = smtp_email_address
    message["Subject"] = date.today().strftime("{} package report for %d/%m/%Y".format(distro_name))

    # Generate message body
    body = "{} package{}, for which you are listed as the maintainer, {} become out of date.\n" \
           "The packages are:{}\n" \
           "See the package report PDF for more information and a full overview of the packages.\n\n" \
           "You are receiving this email as you are listed as a package maintainer.\n"\
           "If you wish to no longer receive these emails, please submit a PR to " \
           "{} to remove yourself as maintainer." \
        .format(len(package_list),
                "s" if len(package_list) != 1 else "",
                "have" if len(package_list) != 1 else "has",
                "\n\t".join(["", *package_list]),
                repo_url)
    message.attach(MIMEText(body, "plain"))

    # Attach PDF file
    message.attach(report_file)

    return message


def generate_maintainerless_email(report_file: MIMEBase, package_list: [str]) -> MIMEMultipart:
    message = MIMEMultipart()
    message["From"] = smtp_email_address
    message["Subject"] = date.today().strftime("{} package report for %d/%m/%Y".format(distro_name))

    # Generate message body
    body = "{} package{}, which have no maintainers, {} become out of date.\n" \
           "The packages are:{}\n" \
           "See the package report PDF for more information and a full overview of the packages.\n\n" \
           "You are receiving this email as you are listed as a package maintainer.\n"\
           "If you wish to no longer receive these emails, contact the host of the package reporter. " \
        .format(len(package_list),
                "s" if len(package_list) != 1 else "",
                "have" if len(package_list) != 1 else "has",
                "\n\t".join(["", *package_list]))
    message.attach(MIMEText(body, "plain"))

    # Attach PDF file
    message.attach(report_file)

    return message

# Send all the mails
# Assumes file "latest-report.pdf" is present.
def send_mails(c: sqlite3.Cursor, server: smtplib.SMTP_SSL | smtplib.SMTP, diff: DistroPackageStatusDiff | None,
               current_distro_status: DistroPackageStatus):
    c.execute("SELECT email FROM generic_email_recipients")
    recipients = c.fetchall()

    # Read report PDF file and encode it for mails
    report_file: MIMEBase = MIMEBase("application", "octet-stream")
    with open("latest-report.pdf", "rb") as file:
        report_file.set_payload(file.read())
    encoders.encode_base64(report_file)
    report_file.add_header("Content-Disposition", "attachment; filename=latest-report.pdf")

    # Generate the email
    if send_generic_email:
        print("Sending generic emails")
        for recipient in recipients:
            recipient = recipient[0]
            customized_mail = generate_report_email(report_file, recipient)
            server.sendmail(smtp_email_address, recipient, customized_mail.as_string())

    if send_maintainer_email and diff is not None:
        print("Sending maintainer emails")
        maintainers_package_list: dict = dict()
        maintainerless_packages: [] = []
        # Check the package list
        for package_name in diff.newly_out_of_date_packages:
            package = current_distro_status.getPackage(package_name)
            distro_package = distro.find_package_by_name(package_name)

            package_string = package_name + ": local version is " + package.version + ", latest upstream is " + \
                             package.upstream_version + " (found in " + package.upstream_repo + ")"

            if distro_package.metadata.maintainer is None or distro_package.metadata.maintainer == "":
                maintainerless_packages.append(package_string)
                continue
            email_addr = parseaddr(distro_package.metadata.maintainer)


            # First entry in tuple is name, second is email
            if email_addr[1]:
                if email_addr[1] in maintainers_package_list:
                    maintainers_package_list[email_addr[1]].append(package_string)
                else:
                    maintainers_package_list[email_addr[1]] = [package_string]

        # Now send the mails
        for maintainer in maintainers_package_list.keys():
            message = generate_maintainer_email(report_file, maintainers_package_list[maintainer])
            message["To"] = maintainer
            message["Bcc"] = maintainer
            server.sendmail(smtp_email_address, maintainer, message.as_string())

        # If there are packages with no maintainer, send it to a fallback if present
        if len(maintainerless_packages) and no_maintainer_fallback_email:
            message = generate_maintainerless_email(report_file, maintainerless_packages)
            message["To"] = no_maintainer_fallback_email
            message["Bcc"] = no_maintainer_fallback_email
            server.sendmail(smtp_email_address, no_maintainer_fallback_email, message.as_string())

def main():
    database = sqlite3.connect("packages.db")
    perform_init()

    if maintain_xbdistro_sqllite_database:
        print("Creating XBDistro Tool SQLite database")
        xbdistro_sql = XBStrapSQLite.XBStrapSQLite(distro, "xbdistro.db")
        xbdistro_sql.update_database()

    with closing(database.cursor()) as c:
        perform_db_init(c)

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
        c.execute("SELECT json_distro_state_b64 FROM previous_check_json ORDER BY unix_timestamp DESC LIMIT 1")
        base64_encoded = c.fetchone()
        if base64_encoded is not None:
            previous_check = DistroPackageStatus.fromJSON(base64.b64decode(base64_encoded[0]))
            diff = DistroPackageStatusDiff(current_distro_status, previous_check)
    pprint(diff)

    with closing(database.cursor()) as c:
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

    # Now do the mail sending, if needed
    if send_emails:
        with closing(database.cursor()) as c:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if smtp_is_secure:
                    server.ehlo()
                    server.starttls(context=ssl_context)
                    server.ehlo()
                if smtp_do_auth:
                    server.login(smtp_login_user, smtp_login_password)
                send_mails(c, server, diff, current_distro_status)


if __name__ == '__main__':
    main()

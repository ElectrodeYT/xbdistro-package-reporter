import base64
from pprint import pprint
from flask import Flask, json, jsonify
from flask_caching import Cache
from contextlib import closing
from threading import Lock
import sqlite3
import libversion
import json

# Random code generation
import random
import string

# Email
import smtplib
import ssl
from email import encoders
from email.utils import parseaddr
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Local things
from Common import *

## Settings
# Email to display in some errors as a contact
contact_email: str = "fillmein@test.com"
# URL where this server is supposed to be accessed from, without trailing slash
server_url: str = "127.0.0.1:5000"
# Name of distribution
distro_name: str = "Managarm"

## Send emails
# Enable email related requests.
allow_email_config: bool = True
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
app: Flask = Flask(__name__)

database = sqlite3.connect("packages.db", check_same_thread=False)
# We need to lock the database manually when we write
database_write_lock: Lock = Lock()

# We do not maintain a lock for this database since we do not write to it
xbdistro_database = sqlite3.connect("xbdistro.db", check_same_thread=False)

ssl_context = ssl.create_default_context()


# We can't import this from main due to flask limitations
# Therefore, copy this over with some modifications
# I know this is turbo ugly, but it will have to do
@dataclass
class DistroPackageStatus:
    packages: [DistroPackage]

    def __init__(self):
        self.packages = []

    def toJSON(self):
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)

    @staticmethod
    def fromJSON(json_data: str):
        ret: DistroPackageStatus = DistroPackageStatus()
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


def send_text_email(dest: str, content: str, subject: str):
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        if smtp_is_secure:
            server.ehlo()
            server.starttls(context=ssl_context)
            server.ehlo()
        if smtp_do_auth:
            server.login(smtp_login_user, smtp_login_password)

        message = MIMEMultipart()
        message["To"] = dest
        message["From"] = smtp_email_address
        message["Subject"] = subject
        message.attach(MIMEText(content, "plain"))

        server.sendmail(smtp_email_address, dest, message.as_string())


def generate_code():
    letters = string.ascii_letters + string.digits
    return "".join(random.choice(letters) for i in range(32))


@app.route("/")
def api_doc():
    return "<h1>API Base URL</h1><p>This is the root URL of the API for the xbdistro-package-reporter.</p><br>" \
           "<p>Calls under /email return human-readable HTML, and are not supposed to be machine-interpreted.</p>" \
           "<p>Other calls return JSON. These contain at least a \"status\" string, and if status is not 200, a" \
           "error string.<br></p>\n" \
           "<h2>/packages/list</h2>" \
           "<p>Returns an array of packages in \"packages\". Each package contains at least \"name\", \"version\", and" \
           "\"origin\". Name and version are strings, while origin is a map of \"file\" and \"line\". If the package" \
           "was found in an upstream repository, it will also contain \"upstream_repo\", \"upstream_version\", " \
           "and \"is_up_to_date\".</p>" \
           "<h2>/packages/package/&lt;name&gt;</h2>" \
           "<p>Returns more information about a specific package. Specifically, it returns " \
           "\"revision\", \"file\", \"line\", \"source\", which contains \"name\" (the name of the source), " \
           "\"version\", \"file\" and \"line\", and \"metadata\", which contains \"maintainer\", \"file\" and " \
           "\"line\". <br> This follows the YAML schema.</p>" \
           "<h2>/checks/history</h2>" \
           "<p>Returns an array \"checks\", where each check contains the \"amount_out_of_date\", \"amount_total\" " \
           "and \"unix_timestamp\".</p>"


@app.route("/packages/list")
def get_package_list():
    return_object: dict = dict()

    with closing(database.cursor()) as c:
        # Not locked - Cannot issue writes
        c.execute("SELECT json_distro_state_b64 FROM previous_check_json ORDER BY unix_timestamp DESC LIMIT 1")
        base64_encoded = c.fetchone()
        if base64_encoded is None:
            return_object["status"] = 500
            return_object["message"] = "Failed to find last check in database"
            return jsonify(return_object)
        previous_check: DistroPackageStatus = DistroPackageStatus.fromJSON(base64.b64decode(base64_encoded[0]))

    name_list: [] = []
    for package in previous_check.packages:
        package_dict: dict = dict(name=package.package,
                                  version=package.version,
                                  origin=dict(file=package.file, line=package.line))
        if package.found_upstream:
            package_dict["upstream_version"] = package.upstream_version
            package_dict["upstream_repo"] = package.upstream_repo
        # TODO: do we save this in the database?
        #       at the moment we dont to save space, as the encoding isnt that great,
        #       however depending on what the performance of this is ends up being, we might want to consider saving this
        # Check if package is up to date
        if package.found_upstream:
            package_dict["is_up_to_date"] = True if libversion.version_compare2(package.version,
                                                                           package.upstream_version) >= 0 else False
        name_list.append(package_dict)

    return_object["status"] = 200
    return_object["count"] = len(name_list)
    return_object["packages"] = name_list

    return jsonify(return_object)


@app.route("/packages/package/<name>")
def get_more_package_info(name):
    with closing(xbdistro_database.cursor()) as c:
        # Not locked - Cannot issue writes
        c.execute("SELECT source_name, revision, maintainer FROM packages WHERE name = ?", [name])
        xbdistro_package_data = c.fetchone()
        pprint(xbdistro_package_data)
        if xbdistro_package_data is None:
            return jsonify(dict(status=500, message="Failed to find package in xbdistro database"))
        c.execute("SELECT type, version FROM sources WHERE source_name = ?", [xbdistro_package_data[0]])
        xbdistro_source_data = c.fetchone()
        pprint(xbdistro_source_data)
        if xbdistro_package_data is None:
            return jsonify(dict(status=500, message="Failed to find source in xbdistro database"))

        # Get the list of dependencies
        c.execute("SELECT package_depend FROM package_dependencies WHERE package_name = ?", [name])
        dependencies: [str] = []
        for data in c.fetchall():
            dependencies.append(data[0])

        # Get the list of dependent packages
        c.execute("SELECT package_name FROM package_dependencies WHERE package_depend = ?", [name])
        dependent: [str] = []
        for data in c.fetchall():
            dependent.append(data[0])

        # Get the needed information from the file_lines table
        c.execute("SELECT file, line, entry FROM file_lines WHERE package_name = ? OR package_name = ?",
                  [name, "__source__" + xbdistro_package_data[0]])
        xbdistro_line_info = c.fetchall()
        pprint(xbdistro_line_info)
        if xbdistro_line_info is None or len(xbdistro_line_info) != 3:
            return jsonify(dict(status=500, message="Failed to find file line info in xbdistro database"))
        for file_line_entry in xbdistro_line_info:
            if file_line_entry[2] == "source_def":
                source_line = file_line_entry
            elif file_line_entry[2] == "meta_def":
                meta_line = file_line_entry
            elif file_line_entry[2] == "main_def":
                main_line = file_line_entry
        if "source_line" not in locals():
            return jsonify(dict(status=500, message="Failed to find source file line info in xbdistro database"))
        if "meta_line" not in locals():
            return jsonify(dict(status=500, message="Failed to find meta file line info in xbdistro database"))
        if "main_line" not in locals():
            return jsonify(dict(status=500, message="Failed to find main file line info in xbdistro database"))
    return jsonify(dict(status=200,
                        source=dict(name=xbdistro_package_data[0],
                                    version=xbdistro_source_data[1],
                                    file=source_line[0],
                                    line=source_line[1]),
                        revision=xbdistro_package_data[1],
                        metadata=dict(maintainer=xbdistro_package_data[2],
                                      file=meta_line[0],
                                      line=meta_line[1]),
                        dependencies=dependencies,
                        dependent=dependent,
                        file=main_line[0],
                        line=main_line[1]))


@app.route("/checks/history")
def get_check_history():
    return_object: dict = dict(status=200, checks=[])

    with closing(database.cursor()) as c:
        # Not locked - Cannot issue writes
        c.execute("SELECT amount_ood, amount, unix_timestamp FROM check_metadata ORDER BY unix_timestamp DESC")
        sql_data = c.fetchall()
        for check in sql_data:
            return_object["checks"].append(dict(amount_out_of_date=check[0],
                                                amount_total=check[1],
                                                unix_timestamp=check[2]))

    return jsonify(return_object)


# Email requests.
# These are designed to be fired from a webbrowser directly; they therefore do not respond with JSON; but with HTML.
@app.route("/email/unsub/begin/<email>")
def begin_unsubscribe_email(email):
    if not allow_email_config:
        return "<p>This server is not configured to allow email settings to be changed.</p>"
    code = None
    with closing(database.cursor()) as c:
        # Not locked - Cannot issue writes
        # Check if we are even subscribed
        c.execute("SELECT email from generic_email_recipients WHERE email = ?", [email])
        if c.fetchall() is None:
            return "<p>Not subscribed.</p>"
        # Check if an email unsubscription is already in progress
        # If it is, reuse the code so that all generated links are the same
        c.execute("SELECT code FROM generic_email_unsubscribe_key WHERE email = ?", [email])
        code_check = c.fetchone()
        if code_check is not None:
            code = code_check[0]

    if code is None:
        # Generate a code and store it
        code = generate_code()
        with database_write_lock:
            with closing(database.cursor()) as c:
                c.execute("INSERT INTO generic_email_unsubscribe_key(code, email) VALUES(?, ?)", [code, email])
                database.commit()

    # Send a email with the confirmation link
    send_text_email(email,
                    "Someone (hopefully you!) has requested an unsubscription key for the {} package reporter.\n"
                    "To confirm this and stop receiving emails, please click the following link: {}"
                    .format(distro_name, server_url + "/email/unsub/confirm/" + code),
                    "{} package report email unsubscription confirmation".format(distro_name))
    return "<p>Confirmation email sent. Please check your inbox!</p>"


@app.route("/email/unsub/confirm/<code>")
def confirm_unsubscribe_email(code):
    if not allow_email_config:
        return "<p>This server is not configured to allow email settings to be changed.</p>"
    # Check if the code exists
    with closing(database.cursor()) as c:
        # Not locked - Cannot issue writes
        c.execute("SELECT email FROM generic_email_unsubscribe_key WHERE code = ?", [code])
        email = c.fetchone()
        if email is None:
            return "<p>Invalid URL!<br>If this error persists, please contact mailto:{}</p>".format(contact_email)
        email = email[0]
    with database_write_lock:
        with closing(database.cursor()) as c:
            c.execute("DELETE FROM generic_email_recipients WHERE email = ?", [email])
            c.execute("DELETE FROM generic_email_unsubscribe_key WHERE code = ? OR email = ?", [code, email])
            database.commit()
    send_text_email(email, "This message is to confirm that you have been unsubscribed from regular package report"
                           "emails. No further action is required from your end.", "Unsubscribe Conformation")
    return "<p>You ({}) have been unsubscribed from regular package update emails.</p>".format(email)


@app.route("/email/sub/begin/<email>")
def begin_email_subscribe(email):
    if not allow_email_config:
        return "<p>This server is not configured to allow email settings to be changed.</p>"
    code = None
    # Check if an email subscription is already in progress
    # If it is, reuse the code so that all generated links are the same
    with closing(database.cursor()) as c:
        # Not locked - Cannot issue writes
        c.execute("SELECT code FROM generic_email_subscribe_key WHERE email = ?", [email])
        code_check = c.fetchone()
        if code_check is not None:
            code = code_check[0]

    if code is None:
        # Generate a code and store it
        code = generate_code()
        with database_write_lock:
            with closing(database.cursor()) as c:
                c.execute("INSERT INTO generic_email_subscribe_key(code, email) VALUES(?, ?)", [code, email])
                database.commit()

    # Send a email with the confirmation link
    send_text_email(email,
                    "Someone (hopefully you!) has subscribed this email address to the {} package reporter.\n"
                    "To confirm this and begin receiving emails, please click the following link: {}"
                    .format(distro_name, server_url + "/email/sub/confirm/" + code),
                    "{} package report email subscription confirmation".format(distro_name))
    return "<p>Confirmation email sent. Please check your inbox!</p>"


@app.route("/email/sub/confirm/<code>")
def confirm_email_subscribe(code):
    if not allow_email_config:
        return "<p>This server is not configured to allow email settings to be changed.</p>"
    # Check if the code exists
    with closing(database.cursor()) as c:
        # Not locked - Cannot issue writes
        c.execute("SELECT email FROM generic_email_subscribe_key WHERE code = ?", [code])
        email = c.fetchone()
        if email is None:
            return "<p>Invalid URL!<br>If this error persists, please contact mailto:{}</p>".format(contact_email)
        email = email[0]

    with database_write_lock:
        with closing(database.cursor()) as c:
            c.execute("INSERT INTO generic_email_recipients(email) VALUES(?)", [email])
            c.execute("DELETE FROM generic_email_subscribe_key WHERE code = ? OR email = ?", [code, email])
            database.commit()
    send_text_email(email, "This message is to confirm that you have been subscribed to regular package report"
                           "emails. No further action is required from your end.", "Subscription Conformation")
    return "<p>You ({}) have been subscribed to regular package update emails.</p>".format(email)

import base64
from pprint import pprint
from flask import Flask, json, jsonify, render_template, request, send_from_directory
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
app = Flask(__name__, static_folder="web-files/static", template_folder="web-files/templates")
cache = Cache(app, config={"CACHE_TYPE": "SimpleCache"})

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



@cache.cached(timeout=10)
def get_previous_check() -> DistroPackageStatus | None:
    with closing(database.cursor()) as c:
        # Not locked - Cannot issue writes
        c.execute("SELECT json_distro_state_b64 FROM previous_check_json ORDER BY unix_timestamp DESC LIMIT 1")
        base64_encoded = c.fetchone()
        if base64_encoded is None:
            return None
        previous_check: DistroPackageStatus = DistroPackageStatus.fromJSON(base64.b64decode(base64_encoded[0]))
        return previous_check


@cache.memoize(30)
def get_extended_package_data(name: str) -> dict:
    print("get_extended_package_data({})".format(name))
    with closing(xbdistro_database.cursor()) as c:
        # Not locked - Cannot issue writes
        c.execute("SELECT source_name, revision, maintainer FROM packages WHERE name = ?", [name])
        xbdistro_package_data = c.fetchone()
        if xbdistro_package_data is None:
            return dict(status=500, message="Failed to find package in xbdistro database")
        c.execute("SELECT type, version FROM sources WHERE source_name = ?", [xbdistro_package_data[0]])
        xbdistro_source_data = c.fetchone()
        if xbdistro_package_data is None:
            return dict(status=500, message="Failed to find source in xbdistro database")

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
        if xbdistro_line_info is None or len(xbdistro_line_info) != 3:
            return dict(status=500, message="Failed to find file line info in xbdistro database")
        for file_line_entry in xbdistro_line_info:
            if file_line_entry[2] == "source_def":
                source_line = file_line_entry
            elif file_line_entry[2] == "meta_def":
                meta_line = file_line_entry
            elif file_line_entry[2] == "main_def":
                main_line = file_line_entry
        if "source_line" not in locals():
            return dict(status=500, message="Failed to find source file line info in xbdistro database")
        if "meta_line" not in locals():
            return dict(status=500, message="Failed to find meta file line info in xbdistro database")
        if "main_line" not in locals():
            return dict(status=500, message="Failed to find main file line info in xbdistro database")
    return dict(status=200,
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
                line=main_line[1])

### Web UI
@app.route("/")
@cache.cached(timeout=120)
def main_page():
    previous_check = get_previous_check()
    if previous_check is None:
        return render_template("error.html", status=500, message="Failed to find last check in database")

    package_list: [] = []
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
        package_list.append(package_dict)

    return render_template("main_page.html", distro_name=distro_name,
                           package_count=len(previous_check.packages),
                           count_out_of_date=len(previous_check.getOutOfDatePackages()),
                           packages=sorted(package_list, key=lambda d: d["name"]))


@app.route("/package/<name>")
@cache.cached(timeout=60)
def package_info_page(name):
    previous_check = get_previous_check()
    if previous_check is None:
        return render_template("error.html", status=500, message="Failed to find last check in database")

    package = None
    for i in previous_check.packages:
        if i.package == name:
            package = dict(name=i.package,
                           version=i.version,
                           origin=dict(file=i.file, line=i.line))
            if i.found_upstream:
                package["upstream_version"] = i.upstream_version
                package["upstream_repo"] = i.upstream_repo
            # TODO: do we save this in the database?
            #       at the moment we dont to save space, as the encoding isnt that great,
            #       however depending on what the performance of this is ends up being, we might want to consider saving this
            # Check if package is up to date
            if i.found_upstream:
                package["is_up_to_date"] = True if libversion.version_compare2(i.version,
                                                                               i.upstream_version) >= 0 else False

    if package is None:
        return render_template("error.html", status=500, message="Failed to find last status of package in the database")

    extended_package_data = get_extended_package_data(name)
    assert "status" in extended_package_data
    if extended_package_data["status"] != 200:
        return render_template("error.html",
                               status=extended_package_data["status"],
                               message=extended_package_data["message"])

    return render_template("package-info-page.html",
                           package=package,
                           extended_package_data=extended_package_data)


@app.route("/latest-report.pdf")
def download_latest_report():
    return send_from_directory(".", "latest-report.pdf")


### Core API
@app.route("/api")
def api_doc():
    return render_template("api-docs.html", distro_name=distro_name, allow_email_config=allow_email_config)


@app.route("/api/packages/list")
def get_package_list():
    return_object: dict = dict()

    previous_check = get_previous_check()
    if previous_check is None:
        return_object["status"] = 500
        return_object["message"] = "Failed to find last check in database"
        return jsonify(return_object)

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


@app.route("/api/packages/package")
def get_more_package_info():
    if "name" not in request.args:
        return jsonify(dict(status=300, message="No <name> argument"))
    return jsonify(get_extended_package_data(request.args["name"]))


@app.route("/api/checks/history")
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


### e-mail handling
@app.route("/email")
def email_page():
    # A simple HTML page with a simple UI to sub/unsub from the automated mailing
    print("rendering email page")
    return render_template("email.html", project_name=distro_name)


# Email requests.
# These are designed to be fired from a webbrowser directly; they therefore do not respond with JSON; but with HTML.
@app.route("/email/unsub/begin")
def begin_unsubscribe_email():
    if not allow_email_config:
        return "<p>This server is not configured to allow email settings to be changed.</p>"

    assert "email" in request.args
    email = request.args["email"]

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
                    .format(distro_name, server_url + "/email/unsub/confirm?code=" + code),
                    "{} package report email unsubscription confirmation".format(distro_name))
    return "<p>Confirmation email sent. Please check your inbox!</p>"


@app.route("/email/unsub/confirm")
def confirm_unsubscribe_email():
    if not allow_email_config:
        return "<p>This server is not configured to allow email settings to be changed.</p>"

    assert "code" in request.args
    code = request.args["code"]

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
                           "emails. No further action is required from your end.", "Unsubscribe Confirmation")
    return "<p>You ({}) have been unsubscribed from regular package update emails.</p>".format(email)


@app.route("/email/sub/begin")
def begin_email_subscribe():
    if not allow_email_config:
        return "<p>This server is not configured to allow email settings to be changed.</p>"

    assert "email" in request.args
    email = request.args["email"]

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
                    .format(distro_name, server_url + "/email/sub/confirm?code=" + code),
                    "{} package report email subscription confirmation".format(distro_name))
    return "<p>Confirmation email sent. Please check your inbox!</p>"


@app.route("/email/sub/confirm")
def confirm_email_subscribe():
    if not allow_email_config:
        return "<p>This server is not configured to allow email settings to be changed.</p>"

    assert "code" in request.args
    code = request.args["code"]

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
                           "emails. No further action is required from your end.", "Subscription Confirmation")
    return "<p>You ({}) have been subscribed to regular package update emails.</p>".format(email)

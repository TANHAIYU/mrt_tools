#!/usr/bin/python
from mrt_tools.settings import *
from mrt_tools.utilities import *
from wstool import multiproject_cli, config_yaml, multiproject_cmd, config as wstool_config
from Crypto.PublicKey import RSA
import re
try:
    from requests.packages import urllib3
    from requests.exceptions import ConnectionError
except ImportError:
    import urllib3
from catkin_pkg import packages
import subprocess
import gitlab
import click
import yaml
import sys
import os

urllib3.disable_warnings()

# Ugly test to test for bash completion mode
try:
    os.environ['COMP_WORDS']
    is_bashcompletion = True
except KeyError:
    is_bashcompletion = False


# Test whether ros is sourced
if "LD_LIBRARY_PATH" not in os.environ or "/opt/ros" not in os.environ["LD_LIBRARY_PATH"]:
    print "ROS_ROOT not set. Source /opt/ros/<dist>/setup.bash"
    sys.exit(1)


class Git:
    def __init__(self, token=None, host=default_host):
        # Host URL
        self.host = host
        self.token = token
        self.server = None
        self.ssh_key = None

        if is_bashcompletion:
            self.connect()
        else:
            self.test_and_connect()

    def test_and_connect(self):
        # Token
        if self.token is None:
            self.token = Token()
        elif isinstance(self.token, str):
            self.token = Token(path=self.token)
        elif isinstance(self.token, Token):
            pass
        else:
            click.secho("Can't create a token from " + str(type(self.token)), fg="red")

        # Test whether git is configured
        get_userinfo()

        # Connect
        self.connect()

        # Test ssh key
        if not self.check_ssh_key():
            # SSH Key not on server yet. Ask user
            click.echo("No ssh key match found. Which ssh key should we use?")
            local_keys = self.get_local_ssh_keys()
            user_choice = get_user_choice([key.name for key in local_keys], default="Create new key.")
            if user_choice is None:
                self.ssh_key = SSHkey()
                self.ssh_key.create()
            else:
                self.ssh_key = local_keys[user_choice]
            self.upload_ssh_key()

    def connect(self):
        """Connects to the server"""
        try:
            self.server = gitlab.Gitlab(self.host, token=self.token.token)
        except gitlab.exceptions.HttpError:
            click.secho("There was a problem logging in to gitlab. Did you use your correct credentials?", fg="red")
        except ValueError:
            click.secho("No connection to server. Did you connect to VPN?", fg="red")
        except ConnectionError:
            click.secho("No internet connection. Could not connect to server.", fg="red")

    def check_ssh_key(self):
        """Test for the presence and functionality of a ssh-key."""
        local_keys = self.get_local_ssh_keys()
        remote_keys = self.server.getsshkeys()
        if [key for key in local_keys if key.public_key in [r["key"] for r in remote_keys]]:
            return True
        else:
            return False

    def upload_ssh_key(self):
        """Add ssh key to gitlab user account"""
        click.echo("Uploading key " + self.ssh_key.name)
        self.server.addsshkey(self.ssh_key.name, self.ssh_key.public_key)

    def get_namespaces(self):
        """Returns a list of all namespaces in Gitlab"""

        click.echo("Retrieving namespaces...")
        namespaces = {project['namespace']['name']: project['namespace']['id'] for project in self.get_repos()}
        user_name = self.server.currentuser()['username']
        if user_name not in namespaces.keys():
            namespaces[user_name] = 0  # The default user namespace_id will be created with first user project
        return namespaces

    def get_repos(self):
        """Returns a list of all repositories in Gitlab"""

        return list(self.server.getall(self.server.getprojects, per_page=100))

    def find_repo(self, pkg_name, ns=None):
        """Search for a repository within gitlab."""

        click.secho("Search for package " + pkg_name, fg='red')
        results = self.server.searchproject(pkg_name)

        if ns is not None:
            try:
                return next(
                    x["ssh_url_to_repo"] for x in results if x["path_with_namespace"] == str(ns) + "/" + pkg_name)
            except StopIteration:
                return None

        exact_hits = [res for res in results if res["name"] == pkg_name]
        count = len(exact_hits)

        if count is 0:
            # None found
            click.secho("Package " + pkg_name + " could not be found.", fg='red')
            return None
        if count is 1:
            # Only one found
            user_choice = 0
        else:
            # Multiple found
            print "More than one repo with \"" + str(pkg_name) + "\" found. Please choose:"
            user_choice = get_user_choice([item["path_with_namespace"] for item in exact_hits])

        ssh_url = exact_hits[user_choice]['ssh_url_to_repo']
        click.secho("Found " + exact_hits[user_choice]['path_with_namespace'], fg='green')

        return ssh_url

    def create_repo(self, pkg_name):
        """
        This function creates a new repository on the gitlab server.
        It lets the user choose the namespace and tests whether the repo exists already.
        """
        # Dialog to choose namespace
        click.echo("Available namespaces in gitlab, please select one for your new project:")
        namespaces = self.get_namespaces()
        user_choice = get_user_choice(namespaces)
        click.echo("Using namespace '" + namespaces.keys()[int(user_choice)] + "'")
        ns_id = namespaces.values()[int(user_choice)]

        # Check whether repo exists
        ssh_url = self.find_repo(pkg_name, namespaces.keys()[int(user_choice)])

        if ssh_url is not None:
            click.secho("    ERROR Repo exist already: " + ssh_url, fg='red')
            sys.exit(1)

        # Create repo
        if ns_id == 0:  # Create new user namespace
            response = self.server.createproject(pkg_name)
        else:
            response = self.server.createproject(pkg_name, namespace_id=ns_id)
        if not response:
            click.secho("There was a problem with creating the repo.", fg='red')
            sys.exit(1)

        # Return URL
        click.echo("Repository URL is: " + response['ssh_url_to_repo'])
        return response['ssh_url_to_repo']

    @staticmethod
    def get_local_ssh_keys(path=default_ssh_path):
        path = os.path.expanduser(path)
        keys = []
        try:
            for filename in os.listdir(path):
                key = SSHkey(name=filename, dir_path=path)
                if key.load():
                    keys.append(key)
        except OSError:
            pass
        return keys


class SSHkey:
    """The ssh-key is an authentication key for communicating with the gitlab server through the git cli-tool."""

    def __init__(self, name="mrtgitlab", key="", dir_path=default_ssh_path):
        self.name = name
        self.secret_key = ""
        self.dir_path = os.path.expanduser(dir_path)
        self.path = self.dir_path + "/" + self.name
        self.public_key = key

    def load(self):
        """Load from file"""
        try:
            # Secret key
            with open(self.path, 'r') as f:
                self.secret_key = f.read().splitlines()
                while type(self.secret_key) is list:
                    self.secret_key = self.secret_key[0]

            # Public key
            with open(self.path + ".pub", 'r') as f:
                self.public_key = f.read().splitlines()
                while type(self.public_key) is list:
                    self.public_key = self.public_key[0]

            return True
        except (IOError, OSError):
            return False

    def write(self):
        """Write key to file"""
        from os import chmod

        # Choose key file
        while os.path.exists(self.path):
            key_file = click.prompt("Please enter a new key name: ")
            self.path = os.path.expanduser(self.dir_path + key_file)

        # Write keys
        if not os.path.exists(os.path.dirname(self.path)):
            os.makedirs(os.path.dirname(self.path))
        if self.secret_key:
            with open(self.path, 'w') as f:
                chmod(self.path, 0600)
                f.write(self.secret_key)
        if self.public_key:
            with open(self.path + ".pub", 'w') as f:
                chmod(self.path, 0600)
                f.write(self.public_key)
        subprocess.call("eval '$(ssh-agent -s)'", shell=True)
        subprocess.call("ssh-add "+self.path, shell=True)
        click.echo("Wrote key to " + self.path + "(.pub)")

    def create(self):
        """Create new SSH key"""
        click.echo("Generating new SSH Key")
        key = RSA.generate(2048)
        self.secret_key = key.exportKey('PEM')
        self.public_key = key.publickey().exportKey('OpenSSH')
        self.write()


class Token:
    """
    The token file is an authentication key for communicating with the gitlab server through the python API.
    """

    def __init__(self, path=default_token_path, allow_creation=True):
        self.path = os.path.expanduser(path)
        self.token = self.load(self.path)
        if not self and allow_creation:
            self.create()

    def __nonzero__(self):
        return self.token != ""

    @staticmethod
    def load(path):
        """
        Read in the token from a specified path
        """
        try:
            return os.read(os.open(path, 0), 20)
        except (IOError, OSError):
            return ""

    def create(self):
        """
        Create a new token from Gitlab user name and password.
        Normally this function has to be called only once.
        From then on, the persistent token file is used to communicate with the server.
        """
        click.echo("No existing gitlab token file found. Creating new one...")

        tmp_git_obj = gitlab.Gitlab(default_host)
        gitlab_user = None
        while gitlab_user is None:
            try:
                username = click.prompt("Gitlab user name")
                password = click.prompt("Gitlab password", hide_input=True)
                tmp_git_obj.login(username, password)
                gitlab_user = tmp_git_obj.currentuser()
            except gitlab.exceptions.HttpError:
                click.secho("There was a problem logging in to gitlab. Did you use your correct credentials?", fg="red")
            except ValueError:
                click.secho("No connection to server. Did you connect to VPN?", fg="red")
            except ConnectionError:
                click.secho("No connection to server. Are you connected to the internet?", fg="red")


        self.token = gitlab_user['private_token']
        self.write()

    def write(self):
        """Write to file"""
        if not os.path.exists(os.path.dirname(self.path)):
            os.makedirs(os.path.dirname(self.path))

        with open(self.path, 'w') as f:
            f.write(self.token)

        click.echo("Token written to: " + self.path)


class Workspace:
    """Object representing a catkin workspace"""

    def __init__(self, init=False):
        self.root = self.get_root()
        if init:
            self.create()
        elif self.root is None:
            raise Exception("No catkin workspace root found.")
        self.src = self.root + "/src/"
        self.config = None
        self.updated_apt = False
        self.load()
        self.pkgs = self.get_catkin_packages()
        catkin_pkgs = set(self.get_catkin_package_names())
        wstool_pks = set(self.get_wstool_package_names())
        if not catkin_pkgs.issubset(wstool_pks):
            self.scan()
            click.echo("wstool and catkin found different packages!")

    def create(self):
        """Initialize new catkin workspace"""
        # Test for existing workspace
        if self.root:
            click.secho("Already inside a catkin workspace. Can't create new.", fg="red")
            sys.exit(1)

        # Test whether directory is empty
        if os.listdir("."):
            click.echo(os.listdir("."))
            if not click.confirm("The repository folder is not empty. Would you like to continue?"):
                sys.exit(0)

        click.secho("Creating workspace", fg="green")
        self.root = os.getcwd()
        os.mkdir("src")
        subprocess.call("catkin init", shell=True)
        os.chdir("src")
        subprocess.call("wstool init", shell=True)
        subprocess.call("catkin build", shell=True)
        self.cd_root()

    def exists(self):
        """Test whether workspace exists"""
        return self.get_root() is not None

    @staticmethod
    def get_root():
        """Find the root directory of a workspace, starting from '.' """
        org_dir = os.getcwd()
        current_dir = org_dir
        while current_dir != "/" and current_dir != "":
            if ".catkin_tools" in os.listdir(current_dir):
                break
            current_dir = os.path.dirname(current_dir)

        os.chdir(org_dir)
        if current_dir == "/" or current_dir == "":
            return None
        else:
            return current_dir

    def cd_root(self):
        """Changes directory to workspace root"""
        os.chdir(self.root)

    def cd_src(self):
        """Changes directory to workspace src folder"""
        os.chdir(self.src)

    def load(self):
        """Read in .rosinstall from workspace"""
        self.config = multiproject_cli.multiproject_cmd.get_config(self.src, config_filename=".rosinstall")

    def write(self):
        """Write to .rosinstall in workspace"""
        config_yaml.generate_config_yaml(self.config, self.src + ".rosinstall", "")

    def add(self, pkg_name, url, update=True):
        """Add a repository to the workspace"""
        ps = config_yaml.PathSpec(pkg_name, "git", url)
        self.config.add_path_spec(ps)
        if update:
            self.write()
            self.update_only(pkg_name)

    def find(self, pkg_name):
        """Test whether package exists"""
        return pkg_name in self.get_wstool_package_names()

    def update(self):
        """Update this workspace"""
        subprocess.call("wstool update -t {0} -j 10".format(self.src), shell=True)

    def update_only(self, pkgs):
        """Update this workspace"""
        jobs = 1
        if isinstance(pkgs, list):
            jobs = len(pkgs)
            jobs = 10 if jobs > 10 else jobs
            pkgs = " ".join(pkgs)
        subprocess.call("wstool update -t {0} -j {1} {2}".format(self.src, jobs, pkgs), shell=True)
        
    def unpushed_repos(self):
        """Search for unpushed commits in workspace"""
        org_dir = os.getcwd()
        unpushed_repos = []
        for ps in self.config.get_config_elements():
            try:
                os.chdir(self.src + ps.get_local_name())
                git_process = subprocess.Popen("git log --branches --not --remotes", shell=True, stdout=subprocess.PIPE)
                result = git_process.communicate()

                if result[0] != "":
                    click.secho("Unpushed commits in repo '" + ps.get_local_name() + "'", fg="yellow")
                    subprocess.call("git log --branches --not --remotes --oneline", shell=True)
                    unpushed_repos.append(ps.get_local_name())
            except OSError: # Directory does not exist (repo not cloned yet)
                pass

        os.chdir(org_dir)
        return unpushed_repos

    def test_for_changes(self):
        """ Test workspace for any changes that are not yet pushed to the server """
        # Parse git status messages
        statuslist = multiproject_cmd.cmd_status(self.config, untracked=True)
        statuslist = [{k["entry"].get_local_name(): k["status"]} for k in statuslist if k["status"] != ""]

        # Check for unpushed commits
        unpushed_repos = self.unpushed_repos()

        # Prompt user if changes detected
        if len(unpushed_repos) > 0 or len(statuslist) > 0:
            if len(statuslist) > 0:  # Unpushed repos where asked already
                click.secho("\nYou have the following uncommited changes:", fg="red")
                for e in statuslist:
                    click.echo(e.keys()[0])
                    click.echo(e.values()[0])

            click.confirm("Are you sure you want to continue to create a snapshot?" +
                          " These changes won't be included in the snapshot!", abort=True)

    def snapshot(self, filename):
        """Writes current workspace configuration to file"""
        source_aggregate = multiproject_cmd.cmd_snapshot(self.config)
        with open(filename, 'w') as f:
            f.writelines(yaml.safe_dump(source_aggregate))

    def get_catkin_packages(self):
        """Returns a dict of all catkin packages"""
        return packages.find_packages(self.src)

    def get_catkin_package_names(self):
        """Returns a list of all catkin packages in ws"""
        return [k for k, v in self.pkgs.items()]

    def get_wstool_package_names(self):
        """Returns a list of all wstool packages in ws"""
        return [pkg.get_local_name() for pkg in self.config.get_config_elements()]

    def get_dependencies(self, pkg_name, deep=False):
        """Returns a dict of all dependencies"""
        if pkg_name in self.pkgs.keys():
            deps = [d.name for d in self.pkgs[pkg_name].build_depends]
            if len(deps) > 0:
                if deep:
                    deps = [self.get_dependencies(d, self.pkgs) for d in deps]
                return {pkg_name: deps}
            else:
                return {pkg_name: []}
        else:
            return pkg_name

    def get_all_dependencies(self):
        """Returns a flat list of dependencies"""
        return set(
            [build_depend.name for catkin_pkg in self.pkgs.values() for build_depend in catkin_pkg.build_depends])

    def resolve_dependencies(self, git=None):
        # TODO maybe use rosdep2 package directly
        if not git:
            git = Git()

        regex_rosdep_resolve = re.compile("ERROR\[([^\]]*)\]: Cannot locate rosdep definition for \[([^\]]*)\]")

        while True:
            rosdep_process = subprocess.Popen(['rosdep', 'check', '--from-paths', self.src, '--ignore-src'],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rosdep_output, rosdep_err = rosdep_process.communicate()

            if not rosdep_err:
                break

            missing_packages = dict()
            for match in regex_rosdep_resolve.finditer(rosdep_err):
                missing_packages[match.group(2)] = match.group(1)

            if not missing_packages:
                print rosdep_output
                print rosdep_err
                sys.exit(1)

            gitlab_packages = []
            for missing_package, package_dep_specified in missing_packages.iteritems():
                # Search for package in gitlab
                url = git.find_repo(missing_package)
                if url:
                    self.add(missing_package, url, update=False)
                    gitlab_packages.append(missing_package)
                else:
                    # no Gitlab project found
                    if not self.updated_apt:
                        # first not found package. Update apt-get and ros.
                        click.secho("Updating mrt apt-get and rosdep and resolve again. This might take a while ...",
                                    fg='green')
                        update_apt_and_ros_packages()
                        self.updated_apt = True
                        break
                    else:
                        click.secho(
                            "Package {0} (requested from: {1}) could not be found.".format(missing_package,
                                                                                           package_dep_specified),
                            fg='red')
                        sys.exit(1)
            # Load new gitlab packages
            self.write()
            self.update_only(gitlab_packages)

        # install missing system dependencies
        subprocess.check_call(["rosdep", "install", "--from-paths", self.src, "--ignore-src"])

    def scan(self, write=True):
        """Goes through all directories within the workspace and checks whether the rosinstall file is up to date."""
        self.pkgs = self.get_catkin_packages()
        self.config = wstool_config.Config([], self.src)
        self.cd_src()
        for pkg in self.pkgs.keys():
            try:
                # Try to read it from package xml
                if len(self.pkgs[pkg].urls) > 1:
                    raise IndexError
                ssh_url = self.pkgs[pkg].urls[0].url
            except IndexError:
                click.secho("Warning: No URL (or multiple) defined in src/" + pkg + "/package.xml!", fg="yellow")
                try:
                    # Try reading it from git repo
                    with open(pkg + "/.git/config", 'r') as f:
                        ssh_url = next(line[7:-1] for line in f if line.startswith("\turl"))
                        # fix_package_xml(self.src + "/" + pkg + "/package.xml", ssh_url)
                except IOError:
                    click.secho("Warning: Could not figure out any URL for " + pkg, fg="red")
                    ssh_url = None
            self.add(pkg, ssh_url, update=False)

        # Create rosinstall file from config
        if write:
            self.write()


def fix_package_xml(filename, url):
    with open(filename, 'r') as f:
        contents = f.readlines()
    click.clear()
    for index, item in enumerate(contents):
        click.echo("{0}: {1}".format(index, item))
    linenumber = click.prompt("Line to enter url?", type=click.INT)
    contents.insert(linenumber, '  <url type="repository">{0}</url>\n'.format(url))
    contents = "".join(contents)
    with open(filename, 'w') as f:
        f.write(contents)
    if click.confirm("Commit?"):
        org_dir = os.getcwd()
        os.chdir(os.path.dirname(filename))
        subprocess.call("git add {0}".format(filename), shell=True)
        subprocess.call("git commit -m 'Added repository url to package.xml'", shell=True)
        os.chdir(org_dir)


def export_repo_names():
    """
    Read repo list from server and write it into caching file.
    """
    # Because we are calling this during autocompletion, we don't wont any errors.
    # -> Just exit when something is not ok.
    try:
        # Connect
        token = Token(path=default_token_path, allow_creation=False)
        git = Git(token=token)
        repo_dicts = git.get_repos()
    except:
        # In case the connection didn't succeed, the file is going to be flushed.
        repo_dicts = []

    file_name = os.path.expanduser(default_repo_cache)
    if not os.path.exists(file_name):
        os.makedirs(os.path.dirname(file_name))
    with open(os.path.expanduser(default_repo_cache), "w") as f:
        for r in repo_dicts:
            f.write(r["name"] + ",")


def import_repo_names():
    """
    Try to read in repos from cached file.
    If file is older than default_repo_cache_time seconds, a new list is retrieved from server.
    """
    import time

    now = time.time()
    try:
        # Read in last modification time
        last_modification = os.path.getmtime(os.path.expanduser(default_repo_cache))
    except OSError:
        # Set modification time to 2 * default_repo_cache_time ago
        last_modification = now - 2 * default_repo_cache_time

    # Read new repo list from server if delta_t > 1 Minute
    if (now - last_modification) > default_repo_cache_time:
        export_repo_names()

    # Read in repo list from cache
    with open(os.path.expanduser(default_repo_cache), "r") as f:
        repos = f.read()
    return repos.split(",")[:-1]
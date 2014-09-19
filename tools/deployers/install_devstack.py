#!/usr/bin/env python
from StringIO import StringIO
import argparse
import os
import yaml

from fabric.api import sudo, settings, run, hide, put, shell_env, cd, get
from fabric.contrib.files import exists, append
from fabric.colors import green, red

from utils import warn_if_fail, quit_if_fail, update_time

DESCRIPTION='Installer for Devstack.'
CISCO_TEMPEST_REPO = "https://github.com/CiscoSystems/tempest.git"
DOMAIN_NAME = "domain.name"
LOGS_COPY = {
    "/etc": "etc_configs",
    "/var/log": "all_logs"}

DEVSTACK_TEMPLATE = '''
[[local|localrc]]
MULTI_HOST=1
{services_specific}
LIBVIRT_TYPE=qemu
NOVA_USE_QUANTUM_API=v2
LOGFILE=/tmp/stack.sh.log
VERBOSE=True
DEBUG=True
USE_SCREEN=True
SCREEN_LOGDIR=$DEST/logs
API_RATE_LIMIT=False
FIXED_RANGE_V6=2001:dead:beef:deed::/64
IPV6_NETWORK_GATEWAY=2001:dead:beef:deed::1
REMOVE_PUBLIC_BRIDGE=False
'''
CONTROLLER = '''
HOST_IP={control_node_ip}
disable_service n-net heat h-api h-api-cfn h-api-cw h-eng c-api c-sch c-vol n-novnc
enable_service neutron q-svc q-agt q-dhcp q-l3 q-meta n-cpu q-vpn q-lbaas cinder
{tempest}
ADMIN_PASSWORD=Cisco123
SERVICE_TOKEN=$ADMIN_PASSWORD
DATABASE_PASSWORD=$ADMIN_PASSWORD
RABBIT_PASSWORD=$ADMIN_PASSWORD
SERVICE_PASSWORD=$ADMIN_PASSWORD
MYSQL_PASSWORD=$ADMIN_PASSWORD
SERVICE_TOKEN=1112f596-76f3-11e3-b3b2-e716f9080d50
IP_VERSION={ipversion}
'''
COMPUTE = '''
HOST_IP={compute_node_ip}
SERVICE_HOST={control_node_ip}
MYSQL_HOST={control_node_ip}
RABBIT_HOST={control_node_ip}
GLANCE_HOSTPORT={control_node_ip}:9292
ADMIN_PASSWORD=Cisco123
SERVICE_TOKEN=$ADMIN_PASSWORD
RABBIT_PASSWORD=$ADMIN_PASSWORD
SERVICE_PASSWORD=$ADMIN_PASSWORD
MYSQL_PASSWORD=$ADMIN_PASSWORD
ENABLED_SERVICES=n-cpu,neutron,n-api,q-agt
IP_VERSION={ipversion}
'''
ALLINONE = """[[local|localrc]]
ADMIN_PASSWORD=Cisco123
DATABASE_PASSWORD=$ADMIN_PASSWORD
RABBIT_PASSWORD=$ADMIN_PASSWORD
SERVICE_PASSWORD=$ADMIN_PASSWORD
SERVICE_TOKEN=1112f596-76f3-11e3-b3b2-e716f9080d50
MYSQL_PASSWORD=$ADMIN_PASSWORD
enable_service g-api g-reg key n-api n-crt n-obj n-cpu n-cond cinder c-sch
enable_service c-api c-vol n-sch n-novnc n-xvnc n-cauth horizon rabbit
enable_service mysql q-svc q-agt q-l3 q-dhcp q-meta q-lbaas q-vpn q-fwaas q-metering neutron
disable_service n-net
{tempest}
NOVA_USE_NEUTRON_API=v2
API_RATE_LIMIT=False
VERBOSE=True
DEBUG=True
LOGFILE=~/stack.sh.log
USE_SCREEN=True
SCREEN_LOGDIR=$DEST/logs
IP_VERSION={ipversion}
IPV6_PRIVATE_RANGE=2001:dead:beef:deed::/64
IPV6_NETWORK_GATEWAY=2001:dead:beef:deed::1
REMOVE_PUBLIC_BRIDGE=False
"""


def make_local(filepath, settings, sudo_flag):
    tempest_conf = """enable_service tempest
TEMPEST_REPO={repo}
TEMPEST_BRANCH={branch}"""
    ipversion = "4+6" if settings.get("ipversion") == 64 else str(settings.get("ipversion"))
    tempest = "" if settings.get("tempest_disable") else tempest_conf.format(repo=settings.get("repo"),
                                                                             branch=settings.get("branch"))
    conf = settings.get("local_conf").format(ipversion=ipversion, tempest=tempest,
                                             control_node_ip=settings.get("node_dict").get("control_node_ip"),
                                             compute_node_ip=settings.get("node_dict").get("compute_node_ip"))
    fd = StringIO(conf)
    warn_if_fail(put(fd, filepath, use_sudo=sudo_flag))


def install_devstack(settings_dict, envs=None, verbose=None):
    envs = envs or {}
    verbose = verbose or {}

    def local_get(remote_path, local_path):
        if exists(remote_path):
            get(remote_path, local_path)
        else:
            print (red("No % file, something went wrong! :(" % remote_path))

    with settings(**settings_dict), hide(*verbose), shell_env(**envs):
        if exists("/etc/gai.conf"):
            append("/etc/gai.conf", "precedence ::ffff:0:0/96  100", use_sudo=True)
        if settings_dict.get("proxy"):
            warn_if_fail(put(StringIO('Acquire::http::proxy "http://proxy.esl.cisco.com:8080/";'),
                             "/etc/apt/apt.conf.d/00proxy",
                             use_sudo=True))
            warn_if_fail(put(StringIO('Acquire::http::Pipeline-Depth "0";'),
                             "/etc/apt/apt.conf.d/00no_pipelining",
                             use_sudo=True))
        update_time(sudo)
        if settings_dict.get("ipversion") != 4:
            sudo("/sbin/sysctl -w net.ipv6.conf.all.forwarding=1")
            append("/etc/sysctl.conf", "net.ipv6.conf.all.forwarding=1", use_sudo=True)
        warn_if_fail(sudo("apt-get update"))
        warn_if_fail(sudo("apt-get install -y git python-pip"))
        warn_if_fail(run("git config --global user.email 'test.node@example.com';"
                         "git config --global user.name 'Test Node'"))
        run("rm -rf ~/devstack")
        quit_if_fail(run("git clone https://github.com/openstack-dev/devstack.git"))
        make_local(filepath="devstack/local.conf", sudo_flag=False, settings=settings_dict)
        with cd("devstack"):
            warn_if_fail(run("./stack.sh"))
            if settings_dict.get("patch"):
                warn_if_fail(run("git fetch https://review.openstack.org/openstack-dev/devstack "
                                 "refs/changes/87/87987/12 && git format-patch -1 FETCH_HEAD"))
        if settings_dict.get("host_string") == settings_dict.get("node_dict").get("control_node_ip"):
            local_get('~/devstack/openrc', "./openrc")
            local_get('/opt/stack/tempest/etc/tempest.conf', "./tempest.conf")
        print (green("Finished!"))
        return True


def make_job(job_template, local_conf, node_dict, ssh_key_file):
    job = job_template
    job.update({"host_string": job_template.get("host"),
                "warn_only": True,
                "key_filename": ssh_key_file,
                "abort_on_prompts": True,
                "local_conf": local_conf,
                "node_dict": node_dict})
    return job


def main(**kwargs):
    actual_jobs = []
    local_conf = [ALLINONE]
    verb_mode = []
    if kwargs.get("quiet"):
        verb_mode = ['output', 'running', 'warnings']
    if not kwargs.get("ssh_key_file"):
        ssh_key_file = os.path.join(os.path.dirname(__file__), "..", "libvirt-scripts", "id_rsa")
    else:
        ssh_key_file = kwargs.get("ssh_key_file")
    if not kwargs.get("config_file"):
        nodes = {"control_node_ip": kwargs.get("host"),
                 "compute_node_ip": kwargs.get("host")}
        actual_jobs.append(make_job(kwargs, local_conf[0], nodes, ssh_key_file))
    else:
        config = yaml.load(kwargs.get("config_file"))
        nodes = {"control_node_ip": config['servers']['devstack-server'][0]["ip"],
                 "compute_node_ip": config['servers']['devstack-server'][0]["ip"]}
        if len(config['servers']['devstack-server']) > 1:
            local_conf = [DEVSTACK_TEMPLATE.format(services_specific=CONTROLLER),
                          DEVSTACK_TEMPLATE.format(services_specific=COMPUTE)]
            nodes = {"control_node_ip": config['servers']['devstack-server'][0]["ip"],
                     "compute_node_ip": config['servers']['devstack-server'][1]["ip"]}
        for k, v in zip(config['servers']['devstack-server'], local_conf):
            local_kwargs = kwargs
            local_kwargs.update({"host": k["ip"]})
            local_kwargs.update({"password": kwargs.get("password") or k["password"]})
            local_kwargs.update({"user": kwargs.get("user") or k["user"]})
            actual_jobs.append(make_job(local_kwargs,
                                        ssh_key_file=ssh_key_file,
                                        node_dict=nodes,
                                        local_conf=v))
    for job in actual_jobs:
        if install_devstack(settings_dict=job,
                            verbose=verb_mode):
            print("Job with host {host} finished successfully!".format(host=job.get("host")))


def define_cli(p):
    p.add_argument('-a', dest='host', default=None, help='IP of host in to install Devstack on')
    p.add_argument('-b', dest='branch', nargs="?", default="master-in-use", const="master-in-use",
                   help='Tempest repository branch, default is master-in-use')
    p.add_argument('-c', dest='config_file', default=None,
                   help='Configuration file, default is None', type=argparse.FileType('r'))
    p.add_argument('-g', dest='gateway', default=None, help='Gateway to connect to host')
    p.add_argument('-q', action='store_true', default=False, dest='quiet', help='Make all silently')
    p.add_argument('-k', dest='ssh_key_file', default=None, help='SSH key file, default is from repo')
    p.add_argument('-j', action='store_true', dest='proxy', default=False,
                   help='Use Cisco proxy if installing from Cisco local network')
    p.add_argument('-u', dest='user', help='User to run the script with')
    p.add_argument('-p', dest='password', help='Password for user and sudo')
    p.add_argument('-m', action='store_true', default=False, dest='patch',
                   help='If apply patches to Devstack')
    p.add_argument('--ip-version', dest='ipversion', type=int, default=4,
                   choices=[4, 6, 64], help='IP version in local.conf, default is 4')
    p.add_argument('--disable-tempest', action='store_true', default=False, dest='tempest_disable',
                   help="Don't install tempest on devstack")
    p.add_argument('-r', dest='repo', nargs="?",
                   const=CISCO_TEMPEST_REPO, default=CISCO_TEMPEST_REPO,
                   help='Tempest repository.')
    p.add_argument('--version', action='version', version='%(prog)s 2.0')

    def main_with_args(args):
        main(host=args.host, branch=args.branch, config_file=args.config_file,
             gateway=args.gateway, quiet=args.quiet, ssh_key_file=args.ssh_key_file,
             proxy=args.proxy, user=args.user, password=args.password, patch=args.patch,
             ipversion=args.ipversion, tempest_disable=args.tempest_disable,
             repo=args.repo)

    p.set_defaults(func=main_with_args)

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=DESCRIPTION,
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    define_cli(p)
    args = p.parse_args()
    args.func(args)
#!/usr/bin/env python
import consul
import subprocess
import netifaces
import time
import os
import socket
import json
import hashlib
import glob
import pystache


def read_required_key(c, key):
    index = None
    data = None
    print(key)
    while data == None:
        index, data = c.kv.get(key, index=index)
        if data:
            return data['Value'].decode("utf-8")

def get_current_session(c, zonename, service):
    index = None
    current_session = None
    index, data = c.kv.get('sessions/{0}/{1}'.format(zonename, service))
    if data != None:
            index, data  = c.session.info(current_session)
            if data:
                print(data)
                current_session = data['Value']
    return current_session

def check_service_symlink(service, current_version):
    try:
        path = os.readlink("/opt/helium/{0}/current".format(service))
        if path[-1] == '/':
            path = path[:-1]
        head, tail = os.path.split(path)
        if tail.startswith("{0}-".format(service)):
            return tail[len(service)+1:]
        return None
    except OSError:
        return None

def get_upgrade_session(c, service, zonename):
    ## clean up any stale upgrade sessions for this node
    index, sessions = c.session.list()
    for session in sessions:
        if session['Node'] == zonename and session['Name'] == "{0}-upgrade".format(service):
                c.session.destroy(session["ID"])
    session = c.session.create(name="{0}-upgrade".format(service), lock_delay=0, ttl=3600)
    return session

def get_upgrade_lock(c, service, zonename):
    ## obtain the upgrade lock
    ## first create a session tied only to node health that has a TTL of one hour
    ## if this node crashes, the lock should be released
    session = get_upgrade_session(c, service, zonename)
    locked = False
    while locked == False:
        locked = c.kv.put("service/{0}/upgrade".format(service), zonename, acquire=session)
    return session

def release_upgrade_lock(c, session):
    c.session.destroy(session)

def go_out_of_service(c, cnsname):
    subprocess.call(["mdata-put", "triton.cns.status", "down"])
    c.agent.maintenance(True, "upgrade")

    ## check CNS knows we're down
    foo=netifaces.ifaddresses('net0')
    host_ip=foo[netifaces.AF_INET][0]['addr']
    hostname, aliases, addresses = socket.gethostbyname_ex(cnsname)
    ## XXX this seems to end up using the system resolver, which is 8.8.8.8, which is caching a lot
    return
    print("waiting for CNS to report us down")
    while host_ip in addresses:
        print(host_ip)
        print(addresses)
        time.sleep(5)
        hostname, aliases, addresses = socket.gethostbyname_ex(cnsname)

def enter_service(c):
    subprocess.call(["mdata-put", "triton.cns.status", "up"])
    c.agent.maintenance(False, "upgrade")

def maybe_disable_service(c, service):
    status = subprocess.Popen("svcs -H {0}".format(service), shell=True, stdout=subprocess.PIPE).stdout.read().rstrip()
    if status == '':
        ## service not installed
        return
    subprocess.call(["svcadm", "disable", service])
    ## wait for at least one of the service's checks to go critical
    while True:
        checks = c.agent.checks()
        service_has_checks = False
        for key, value in checks.items():
            if value['ServiceName'] == service:
                service_has_checks = True
        if service_has_checks == False:
                return
        for key, value in checks.items():
            if value['ServiceName'] == service and value['Status'] == "critical":
                return
        time.sleep(5)

def fetch_artefact(service, version):
    ## TODO some goddamn s3 thing, I don't know
    return "{0}-{1}-sunos.tgz".format(service, version)

def install_artefact(service, version, filename):
    subprocess.call(["mkdir", "-p", "/opt/helium/{0}".format(service)])
    subprocess.call(["tar", "-C", "/opt/helium/{0}".format(service), "-xf", filename])
    subprocess.call(["rm", "-f", "/opt/helium/{0}/current".format(service)])
    subprocess.call(["ln", "-sf", "/opt/helium/{0}/{0}-{1}/".format(service, version), "/opt/helium/{0}/current".format(service)])
    ## Run the install hook
    subprocess.call(["/opt/helium/{0}/current/helios/hooks/install.sh".format(service)])

def register_check(c, service, check_filename):
    with open(check_filename) as check_file:
        check = json.load(check_file)
        checkobj = None
        if 'tcp' in check:
            hostport = check['tcp'].split(':')
            checkobj = consul.Check.tcp(hostport[0], int(hostport[1]), check['interval'], timeout=check['timeout'])
        elif 'http' in check:
            checkobj = consul.Check.http(check['http'], check['interval'], timeout=check['timeout'])
        elif 'script' in check:
            checkobj = consul.Check.script(check['script'], check['interval'])

        if checkobj != None:
                c.agent.check.register(check['name'], checkobj, service_id=check['serviceid'])
        print(check)

## this can be used for primary and auxiliary services (like pgbouncer)
def check_service(c, zonename, service, cnsname, primary=False):
    version = read_required_key(c, '{0!s}/version'.format(service))

    services = c.agent.services()
    print(services)
    tags = []
    if service in services:
        tags = services[service]['Tags']

    current_config = None
    current_version = None
    for tag in tags:
            if tag.startswith("config-"):
                    current_config = tag[len("config-"):]
            elif tag.startswith("version-"):
                    current_version = tag[len("version-"):]

   
    current_fs_version = check_service_symlink(service, current_version)
    if current_fs_version == None or current_version != current_fs_version:
        current_version = None

    installed = False
    if version != current_version:
        print("upgrading service to {0}".format(version))
        upgrade_session = get_upgrade_lock(c, service, zonename)
        go_out_of_service(c, cnsname)
        maybe_disable_service(c, service)
        filename = fetch_artefact(service, version)
        install_artefact(service, version, filename)
        installed = True

    ## compute the config SHA and compare it to the one in the tag
    index, configs = c.kv.get("{0}/config".format(service), recurse=True)
    json_config = {}
    for config in configs:
        print(config)
        json_config[config['Key'].split('/')[-1]] = config['Value'].decode("utf-8")
 
    foo=netifaces.ifaddresses('net0')
    host_ip=foo[netifaces.AF_INET][0]['addr']
    json_config['host_ip'] = host_ip
    print(json_config)

    json_data = json.dumps(json_config, sort_keys=True, indent=4, separators=(',', ': '))

    text_file = open("data.json", "w")
    text_file.write(json_data)
    text_file.close()
    config_version = hashlib.sha1(json_data.encode("utf-8")).hexdigest()

    configured = False
    if config_version != current_config or installed == True:
        ## find all .mustache files in /opt/helium/$SERVICE/current and template them
        with open('/opt/helium/{0}/current/helios/default.json'.format(service)) as defaults_file:
             defaults = json.load(defaults_file)
        merged_config = {**defaults, **json_config}
        mustaches = glob.glob("/opt/helium/{0}/current/**/*.mustache".format(service), recursive=True)
        renderer = pystache.Renderer()
        for m in mustaches:
                new_file = renderer.render_path(m, merged_config)
                new_file_name, ext = os.path.splitext(m)
                text_file = open(new_file_name, "w")
                text_file.write(new_file)
                text_file.close()
       
        subprocess.call(["/opt/helium/{0}/current/helios/hooks/config.sh".format(service)])

    current_session = None
    if primary == True:
        current_session = get_current_session(c, zonename, service)
        print(current_session)
    
    if installed == True or configured == True:
        ## import the new service definition, it might have changed
        subprocess.call(["svccfg", "import", "/opt/helium/{0}/current/helios/smf/{0}.xml".format(service)])
        subprocess.call(["svcadm", "enable", service])
        subprocess.call(["svcadm", "clear", service])

        c.agent.service.register(service, tags=["version-{0}".format(version), "config-{0}".format(config_version)])
        checks = glob.glob("/opt/helium/{0}/current/helios/checks/*.json".format(service))
        print(checks)
        for check in checks:
                register_check(c, service, check)
        ## destroy any old leader session
        if current_session:
            c.session.destroy(current_session)
            current_session = None 

        print("waiting for health checks to go green")
        while True:
            checks = c.agent.checks()
            services = []
            for key, value in checks.items():
                if value['ServiceName'] == service:
                    services.append(value)
            if all(s['Status'] == 'passing' for s in services):
                break
            time.sleep(5)
        ## ok, the service is green now, release the upgrade lock and leave maintenance mode
        release_upgrade_lock(c, upgrade_session)
        enter_service(c)

    if current_session == None and primary == True:
        ## create a new leader session using the service's checks
        checks = c.agent.checks()
        servicenames = ['serfHealth']
        for key, value in checks.items():
            if value['ServiceName'] == service:
                servicenames.append(value['CheckID'])
        current_session = c.session.create("{0}-leader".format(service), checks=servicenames, lock_delay=0, ttl=120)
    elif primary == True:
        ## renew the session
        c.session.renew(current_session)
    
    if primary == True:
        locked = c.kv.put("service/{0}/leader".format(service), zonename, acquire=current_session)

def main():
    c = consul.Consul()
    zonename = subprocess.Popen("zonename", shell=True, stdout=subprocess.PIPE).stdout.read().rstrip().decode("utf-8")
    cnsname="helios.kwatz.helium.zone"
    foo=netifaces.ifaddresses('net0')
    host_ip=foo[netifaces.AF_INET][0]['addr']

    print(zonename)
    print(host_ip)

    index = None
    service = read_required_key(c, "{0!s}/services".format(zonename))

    while True:
        check_service(c, zonename, service, cnsname, primary=True)
        time.sleep(5)


if __name__ == '__main__':
    main()

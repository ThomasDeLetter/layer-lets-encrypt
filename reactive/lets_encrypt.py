import os
from subprocess import (
    check_output,
    CalledProcessError,
    STDOUT
)
import random
from shutil import copyfile

from crontab import CronTab

from charmhelpers.core import unitdata
from charmhelpers.core.host import (
    lsb_release,
    service_running,
    service_start,
    service_stop
)

from charmhelpers.core.hookenv import (
    log,
    config,
    open_port,
    status_set,
    charm_dir
)

from charms.reactive import (
    when,
    when_any,
    when_all,
    when_not,
    set_state,
    remove_state
)

from charms import layer
from charms import apt


@when_not('apt.installed.letsencrypt')
def check_version_and_install():
    series = lsb_release()['DISTRIB_CODENAME']
    if not series >= 'xenial':
        log('letsencrypt not supported on series >= %s' % (series))
        status_set('blocked', "Unsupported series < Xenial")
        return
    else:
        apt.queue_install(['letsencrypt'])
        apt.install_queued()
        # open ports during installation to prevent a scenario where
        # we need to wait for the update-status hook to request
        # certificates because Juju hasn't opened the ports yet and
        # no other hook is queued to run.
        open_port(80)
        open_port(443)


@when('config.changed.fqdn')
def config_changed():
    configs = config()
    if configs.changed('fqdn') and configs.previous('fqdn') \
       or configs.get('fqdn'):
        remove_state('lets-encrypt.registered')


@when('apt.installed.letsencrypt')
@when_any(
    'lets-encrypt.certificate-requested',
    'config.set.fqdn',
)
@when_not('lets-encrypt.registered')
@when_not('lets-encrypt.disable')
def register_server():
    configs = config()
    # Get all certificate requests
    requests = unitdata.kv().get('certificate.requests', [])
    if not requests and not configs.get('fqdn'):
        return
    if configs.get('fqdn'):
        requests.append({'fqdn': [configs.get('fqdn')],
                         'contact-email': configs.get('contact-email', '')})

    # If the ports haven't been opened in a previous hook, they won't be open,
    # so opened_ports won't return them.
    ports = opened_ports()
    if not ('80/tcp' in ports or '443/tcp' in ports):
        status_set(
            'waiting',
            'Waiting for ports to open (will happen in next hook)')
        return
    if create_certificates(requests):
        unconfigure_periodic_renew()
        configure_periodic_renew()
        create_dhparam()
        set_state('lets-encrypt.registered')



@when_all(
    'apt.installed.letsencrypt',
    'lets-encrypt.registered',
    # This state is set twice each day by crontab. This
    # handler will be run in the next update-status hook.
    'lets-encrypt.renew.requested',
)
@when_not(
    'lets-encrypt.disable',
    'lets-encrypt.renew.disable',
)
def renew_cert():
    remove_state('lets-encrypt.renew.requested')
    # We don't want to stop the webserver if no renew is needed.
    if no_renew_needed():
        return
    print("Renewing certificate...")
    configs = config()
    fqdn = configs.get('fqdn')
    needs_start = stop_running_web_service()
    open_port(80)
    open_port(443)
    try:
        output = check_output(
            ['letsencrypt', 'renew', '--agree-tos'],
            universal_newlines=True,
            stderr=STDOUT)
        print(output)  # So output shows up in logs
        status_set('active', 'registered %s' % (fqdn))
        set_state('lets-encrypt.renewed')
    except CalledProcessError as err:
        status_set(
            'blocked',
            'letsencrypt renewal failed: \n{}'.format(err.output))
        print(err.output)  # So output shows up in logs
    finally:
        if needs_start:
            start_web_service()


def no_renew_needed():
    # If renew is needed, the following call might fail because the needed
    # ports are in use. We catch this because we only need to know if a
    # renew was attempted, not if it succeeded.
    try:
        output = check_output(
            ['letsencrypt', 'renew', '--agree-tos'], universal_newlines=True)
    except CalledProcessError as error:
        output = error.output
    return "No renewals were attempted." in output


def stop_running_web_service():
    service_name = layer.options('lets-encrypt').get('service-name')
    if service_name and service_running(service_name):
        log('stopping running service: %s' % (service_name))
        service_stop(service_name)
        return True


def start_web_service():
    service_name = layer.options('lets-encrypt').get('service-name')
    if service_name:
        log('starting service: %s' % (service_name))
        service_start(service_name)


def configure_periodic_renew():
    charms_reactive = check_output(['which', 'charms.reactive'], universal_newlines=True, stderr=STDOUT).strip()
    command = (
        'export CHARM_DIR="{}"; {} set_flag lets-encrypt.renew.requested '
        ''.format(os.environ['CHARM_DIR'], charms_reactive))
    cron = CronTab(user='root')
    jobRenew = cron.new(
        command=command,
        comment="Renew Let's Encrypt [managed by Juju]")
    # Twice a day, random minute per certbot instructions
    # https://certbot.eff.org/all-instructions/
    jobRenew.setall('{} 6,18 * * *'.format(random.randint(1, 59)))
    jobRenew.enable()
    cron.write()


def unconfigure_periodic_renew():
    cron = CronTab(user='root')
    jobs = cron.find_comment(comment="Renew Let's Encrypt [managed by Juju]")
    for job in jobs:
        cron.remove(job)
    cron.write()


def create_dhparam():
    copyfile(
        '{}/files/dhparam.pem'.format(charm_dir()),
        '/etc/letsencrypt/dhparam.pem')


def opened_ports():
    output = check_output(['opened-ports'], universal_newlines=True)
    return output.split()


def create_certificates(requests):
    for cert_request in requests:
        # Check if there are no conflicts
        # If a fqdn is already present, do not create a new one
        fqdnpaths = []
        for fqdn in cert_request['fqdn']:
            fqdnpaths.append('/etc/letsencrypt/live/' + fqdn)
        if any([os.path.isdir(f) for f in fqdnpaths]):
            continue  # Cert already exists
        needs_start = stop_running_web_service()

        mail_args = []
        if cert_request['contact-email']:
            mail_args.append('--email')
            mail_args.append(cert_request['contact-email'])
        else:
            mail_args.append('--register-unsafely-without-email')
        try:
            # Agreement already captured by terms, see metadata
            le_cmd = ['letsencrypt', 'certonly', '--standalone', '--agree-tos',
                      '--non-interactive']
            for fqdn in cert_request['fqdn']:
                le_cmd.extend(['-d', fqdn])
            le_cmd.extend(mail_args)
            output = check_output(
                le_cmd,
                universal_newlines=True,
                stderr=STDOUT)
            print(output)  # So output shows up in logs
            status_set('active', 'registered %s' % (fqdn))

        except CalledProcessError as err:
            status_set(
                'blocked',
                'letsencrypt registration failed: \n{}'.format(err.output))
            print(err.output)  # So output shows up in logs
            return False
        finally:
            if needs_start:
                start_web_service()
    return True

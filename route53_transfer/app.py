import csv, sys, time
from datetime import datetime
import itertools
from os import environ
from os.path import join
from boto import route53
from boto.route53.record import Record, ResourceRecordSets

ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", datetime.utcnow().utctimetuple())

class ComparableRecord(object):
    def __init__(self, obj):
        for k, v in obj.__dict__.items():
            self.__dict__[k] = v

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __hash__(self):
        it = (self.name, self.type, self.alias_hosted_zone_id,
              self.alias_dns_name, tuple(sorted(self.resource_records)),
              self.ttl, self.region, self.weight, self.identifier,
              self.failover, self.alias_evaluate_target_health)
        return it.__hash__()

    def to_change_dict(self):
        data = {}
        for k, v in self.__dict__.items():
            if k == 'resource_records':
                continue
            else:
                data[k] = v
        return data


def exit_with_error(error):
    sys.stderr.write(error)
    sys.exit(1)


def get_aws_credentials(params):
    access_key = params.get('--access-key-id') or environ.get('AWS_ACCESS_KEY_ID')
    if params.get('--secret-key-file'):
        with open(params.get('--secret-key-file')) as f:
            secret_key = f.read().strip()
    else:
        secret_key = params.get('--secret-key') or environ.get('AWS_SECRET_ACCESS_KEY')
    return access_key, secret_key


def get_zone(con, zone_name, vpc):


    res = con.get_all_hosted_zones()
    zones = res['ListHostedZonesResponse']['HostedZones']
    zone_list = [z for z in zones
                    if z['Config']['PrivateZone'] == (u'true' if vpc.get('is_private') else u'false')
                        and z['Name'] == zone_name + '.']

    for zone in zone_list:
        data = {}
        data['id'] = zone.get('Id','').replace('/hostedzone/', '')
        data['name'] = zone.get('Name')
        if vpc.get("is_private"):
            z = con.get_hosted_zone(data.get('id'))
            z_vpc_id = z.get('GetHostedZoneResponse',{}).get('VPCs',{}).get('VPC',{}).get('VPCId','')
            if z_vpc_id == vpc.get('id'):
                return data
            else:
                continue
        else:
            return data
    else:
        return None


def create_zone(con, zone_name, vpc):
    con.create_hosted_zone(domain_name=zone_name,
                           private_zone=vpc.get('is_private'),
                           vpc_region=vpc.get('region'),
                           vpc_id=vpc.get('id'),
                           comment='autogenerated by route53-transfer @ {}'.format(ts))
    return get_zone(con, zone_name, vpc)

def group_values(lines):
    records = []
    for _, records in itertools.groupby(lines, lambda row: row[0:2]):
        for __, by_value in itertools.groupby(records, lambda row: row[-3:]):
            recs = list(by_value)  # consume the iterator so we can grab positionally
            first = recs[0]

            record = Record()
            record.name = first[0]
            record.type = first[1]
            if first[2].startswith('ALIAS'):
                _, alias_hosted_zone_id, alias_dns_name = first[2].split(':')
                record.alias_hosted_zone_id = alias_hosted_zone_id
                record.alias_dns_name = alias_dns_name
            else:
                record.resource_records = [r[2] for r in recs]
                record.ttl = first[3]
            record.region = first[4] or None
            record.weight = first[5] or None
            record.identifier = first[6] or None
            record.failover = first[7] or None
            if first[8] == 'True':
                 record.alias_evaluate_target_health = True
            elif first[8] == 'False':
                 record.alias_evaluate_target_health = False
            else:
                record.alias_evaluate_target_health = None

            yield record


def read_lines(file_in):
    reader = csv.reader(file_in)
    lines = list(reader)
    if lines[0][0] == 'NAME':
        lines = lines[1:]
    return lines


def read_records(file_in):
    return list(group_values(read_lines(file_in)))


def skip_apex_soa_ns(zone, records):
    for record in records:
        if record.name == zone['name'] and record.type in ['SOA', 'NS']:
            continue
        else:
            yield record


def comparable(records):
    return {ComparableRecord(record) for record in records}


def get_file(filename, mode):
    ''' Get a file-like object for a filename and mode.

        If filename is "-" return one of stdin or stdout.
    '''
    if filename == '-':
        if mode.startswith('r'):
            return sys.stdin
        elif mode.startswith('w'):
            return sys.stdout
        else:
            raise ValueError('Unknown mode "{}"'.format(mode))
    else:
        return open(filename, mode)


def load(con, zone_name, file_in, **kwargs):
    ''' Send DNS records from input file to Route 53.

        Arguments are Route53 connection, zone name, vpc info, and file to open for reading.
    '''
    vpc = kwargs.get('vpc', {})
    zone = get_zone(con, zone_name, vpc)
    if not zone:
        zone = create_zone(con, zone_name, vpc)

    existing_records = comparable(skip_apex_soa_ns(zone, con.get_all_rrsets(zone['id'])))
    desired_records = comparable(skip_apex_soa_ns(zone, read_records(file_in)))

    to_delete = existing_records.difference(desired_records)
    to_add = desired_records.difference(existing_records)

    if to_add or to_delete:
        changes = ResourceRecordSets(con, zone['id'])
        for record in to_delete:
            change = changes.add_change('DELETE', **record.to_change_dict())
            print "DELETE", record.name, record.type
            for value in record.resource_records:
                change.add_value(value)
        for record in to_add:
            change = changes.add_change('CREATE', **record.to_change_dict())
            print "CREATE", record.name, record.type
            for value in record.resource_records:
                change.add_value(value)

        print "Applying changes..."
        changes.commit()
        print "Done."
    else:
        print "No changes."


def dump(con, zone_name, fout, **kwargs):
    ''' Receive DNS records from Route 53 to output file.

        Arguments are Route53 connection, zone name, vpc info, and file to open for writing.
    '''
    vpc = kwargs.get('vpc', {})

    zone = get_zone(con, zone_name, vpc)
    if not zone:
        exit_with_error("ERROR: {} zone {} not found!".format('Private' if vpc.get('is_private') else 'Public',
                                                              zone_name))

    out = csv.writer(fout)
    out.writerow(['NAME', 'TYPE', 'VALUE', 'TTL', 'REGION', 'WEIGHT', 'SETID', 'FAILOVER', "EVALUATE_HEALTH"])

    records = list(con.get_all_rrsets(zone['id']))
    for r in records:
        if r.alias_dns_name:
            vals = [':'.join(['ALIAS', r.alias_hosted_zone_id, r.alias_dns_name])]
        else:
            vals = r.resource_records
        for val in vals:
            out.writerow([r.name, r.type, val, r.ttl, r.region, r.weight, r.identifier, r.failover, r.alias_evaluate_target_health])
    fout.flush()


def run(params):
    access_key, secret_key = get_aws_credentials(params)
    con = route53.connect_to_region('universal', aws_access_key_id=access_key, aws_secret_access_key=secret_key)
    zone_name = params['<zone>']
    filename = params['<file>']

    vpc = {}
    if params.get('--private'):
        vpc['is_private'] = True
        vpc['region'] = params.get('--vpc-region') or environ.get('AWS_DEFAULT_REGION')
        vpc['id'] = params.get('--vpc-id')
        if not vpc.get('region') or not vpc.get('id'):
            exit_with_error("ERROR: Private zones require associated VPC Region and ID "
                            "(--vpc-region, --vpc-id)".format(zone_name))
    else:
        vpc['is_private'] = False

    if params.get('dump'):
        dump(con, zone_name, get_file(filename, 'w'), vpc=vpc)
    elif params.get('load'):
        load(con, zone_name, get_file(filename, 'r'), vpc=vpc)
    else:
        return 1
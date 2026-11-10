"""Fixed, bounded workstreams: topic routing is never a shared-root-cause claim."""
import json
import copy
import re
from pathlib import Path
from core import Failure


class ManagedCatalog:
    def __init__(self, policy=None):
        self.policy = copy.deepcopy(policy) if policy is not None else json.loads(Path(__file__).with_suffix('.json').read_text())
        p = self.policy
        if not isinstance(p,dict) or type(p.get('version')) is not int or p.get('version') != 1 or p.get('max_open') != 20 or p.get('automatic_create') is not False:
            raise Failure('INVALID_CATALOG_POLICY')
        self.topics = p.get('topics', {})
        expected={'registry','observability','spi','capacity','review','memory','notifications','lms',
                  'harbor','prometheus','probes','calendar','n8n','kuma','network','storage','node',
                  'imgproxy','observer','metricsapi'}
        if not isinstance(self.topics,dict) or set(self.topics)!=expected:
            raise Failure('INVALID_CATALOG_POLICY')
        if not isinstance(self.topics,dict) or not 1 <= len(self.topics) <= 20 or not {'review', 'probes', 'spi'} <= self.topics.keys():
            raise Failure('INVALID_CATALOG_POLICY')
        self.aliases = {}
        self.by_issue = {}
        for key, topic in self.topics.items():
            if not isinstance(topic,dict) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',key):
                raise Failure('INVALID_CATALOG_POLICY')
            if not isinstance(topic.get('members'),list) or not isinstance(topic.get('title'),str) or not 1<=len(topic['title'])<=160:
                raise Failure('INVALID_CATALOG_POLICY')
            number = topic.get('issue')
            if type(number) is not int or number <= 0 or number in self.by_issue or number not in topic.get('members', []):
                raise Failure('INVALID_CATALOG_POLICY')
            self.by_issue[number] = key
            for old in topic['members']:
                if type(old) is not int or old <= 0 or old in self.aliases:
                    raise Failure('INVALID_CATALOG_POLICY')
                self.aliases[old] = number

    def route(self, event, previous=0):
        if previous in self.aliases:
            key = self.by_issue[self.aliases[previous]]
        else:
            labels = event.get('labels', {})
            # Bounded operational identities, not model prose or raw log matching.
            scope = ' '.join(str(labels.get(k, '')) for k in (
                'namespace', 'component', 'service_id', 'deployment', 'statefulset',
                'daemonset', 'container', 'job', 'job_name', 'service_name', 'unit')).lower()
            alert = str(labels.get('alertname', '')).lower()
            if any(x in scope for x in ('spi-triage', 'spi-github', 'n8n-extensions')) or 'spi triage' in alert:
                key = 'spi'
            elif 'observer' in alert or 'observer-membership' in scope: key = 'observer'
            elif 'aggregatedapi' in alert or 'metrics.k8s.io' in scope: key = 'metricsapi'
            elif any(x in scope for x in ('img-proxy', 'imageproxy')) or 'imageproxy' in alert: key = 'imgproxy'
            elif 'harbor' in scope or 'harbor' in alert: key = 'harbor'
            elif 'lms' in scope or 'lmsunavailable' in alert: key = 'lms'
            elif 'uptime-kuma' in scope: key = 'kuma'
            elif any(x in scope for x in ('netbird', 'stun', 'vpn', 'trojan')) or 'vpn' in alert: key = 'network'
            elif 'otel' in scope: key = 'observability'
            elif any(x in scope for x in ('rustfs', 'nfs-', 'storage')): key = 'storage'
            elif 'yandex-calendar' in scope or 'yandex' in alert: key = 'calendar'
            elif 'n8n' in scope or 'n8n' in alert: key = 'n8n'
            elif 'memoryovercommit' in alert: key = 'memory'
            elif any(x in alert for x in ('nodeunreachable', 'nodenotready', 'kubeletinstanceunreachable')): key = 'node'
            elif any(x in alert for x in ('pressure', 'resourcecontention', 'diskspace', 'nodefilesystem')): key = 'capacity'
            elif 'compaction' in alert or 'tsdb' in alert or 'prometheusunavailable' in alert: key = 'prometheus'
            elif 'notification' in alert or 'alertmanager' in alert: key = 'notifications'
            elif any(x in scope for x in ('otel', 'node-exporter', 'monitoring-etcd')) or any(x in alert for x in ('metricsstale', 'retentionmetrics', 'targetdown')): key = 'observability'
            elif any(x in alert for x in ('imagepull', 'registry')): key = 'registry'
            elif (str(labels.get('severity', '')).lower() not in {'critical', 'high'}
                  and any(x in alert for x in ('service', 'podnotready', 'replicasmismatch', 'watchdog'))): key = 'probes'
            else: key = 'review'
        return {'version': 1, 'topic': key, 'issue': self.topics[key]['issue'],
                'reason': 'Fixed workstream routing only; common root cause is not established.'}

    def issue(self, github, route):
        key = route.get('topic')
        if route.get('version') != 1 or key not in self.topics or route.get('issue') != self.topics[key]['issue']:
            raise Failure('INVALID_CATALOG_TARGET')
        row = github.request('GET', '/issues/' + str(route['issue']))
        marker = '<!-- socrat-spi-catalog:v1:' + key + ' -->'
        if (row.get('number') != route['issue'] or row.get('state') != 'open' or 'pull_request' in row
                or not row.get('title', '').startswith('[SPI]') or marker not in (row.get('body') or '')):
            raise Failure('CATALOG_TARGET_UNAVAILABLE')
        # Do not load any comment history into the analysis prompt.
        return {'number': row['number'], 'title': self.topics[key]['title'],
                'body': route['reason'], 'updated_at': row.get('updated_at', ''), 'recent_comments': []}

    def banner(self, route, original_number):
        return ('## Managed workstream\n\nFiled under **' + self.topics[route['topic']]['title']
                + '**. This groups investigation work, not proven root causes. '
                + 'The report retains its independent findings and evidence. '
                + 'Model/legacy suggested issue: ' + str(original_number) + '.\n\n')

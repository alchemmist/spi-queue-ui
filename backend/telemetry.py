"""Bounded Loki raw logs, Tempo traces, and absolute Grafana Explore links.

TelemetryCollector.collect(event, existing_evidence) is meant to run in the
collect stage after Evidence.collect, on the same thread that set
integrations.REQUEST_DEADLINE. It does not publish to GitHub, does not redact
log bodies for publication, and does not change alerts. Missing or failed
telemetry is unknown; it is not evidence that a service is healthy.

Upstream reads go through the existing HTTP client:

    http.request(method, url, headers, timeout=..., limit=...)

Query parameters are encoded into that URL. The client enforces
REQUEST_DEADLINE; this module also stops before further reads once that
deadline, or a fixed request cap, is exhausted.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone

from core import SECRET_KEY, Failure
from integrations import REQUEST_DEADLINE

PUBLIC_GRAFANA_URL = 'https://grafana.socrat.alchemmist.xyz'
DATASOURCE_UIDS = ('loki', 'prometheus', 'tempo')

# Incident window: context around [startsAt, endsAt], clipped to the collector
# clock and a fixed lookback. API parameters use whole Unix seconds. Grafana
# Explore links use the same instants as absolute milliseconds.
WINDOW_LEAD_SECONDS = 300
WINDOW_TRAIL_SECONDS = 300
MAX_LOOKBACK_SECONDS = 6 * 3600
MAX_WINDOW_SECONDS = 3600
CONTEXT_RADIUS_SECONDS = 15

ERROR_LINE_LIMIT = 40
STACK_LINE_LIMIT = 40
CONTEXT_LINE_LIMIT = 20
CONTEXT_ANCHORS = 3
LINE_CHAR_LIMIT = 2000
SEARCH_LIMIT = 5
MAX_TRACE_FETCHES = 2
MAX_TRACE_SPANS = 20
MAX_SPANS_SCANNED = 2000
MAX_ATTRIBUTES = 8
ATTRIBUTE_CHAR_LIMIT = 180
MAX_REQUESTS = 10
LOG_RESPONSE_LIMIT = 400_000
TRACE_RESPONSE_LIMIT = 500_000
IDENTITY_RESPONSE_LIMIT = 100_000
REQUEST_TIMEOUT_SECONDS = 15
DETAIL_CHAR_LIMIT = 240

ERROR_LINE_FILTER = '|~ "(?i)error|exception|failed|fatal|panic"'
STACK_LINE_FILTER = '|~ "(?i)traceback|goroutine [0-9]+|stack trace|caused by:|panic:"'

LOKI_RANGE_PATH = '/api/datasources/proxy/uid/loki/loki/api/v1/query_range'
TEMPO_SEARCH_PATH = '/api/datasources/proxy/uid/tempo/api/search'
TEMPO_TRACE_PREFIX = '/api/datasources/proxy/uid/tempo/api/traces/'
PROMETHEUS_QUERY_PATH = '/api/datasources/proxy/uid/prometheus/api/v1/query'

NAMESPACE_RE = re.compile(r'[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z')
POD_RE = re.compile(
    r'(?=.{1,253}\Z)[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*\Z')
CONTAINER_RE = NAMESPACE_RE
HOST_RE = re.compile(
    r'(?=.{1,253}\Z)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    r'(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z')
UNIT_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9:_.@+-]{0,200}\.(?:service|timer|socket)\Z')
SERVICE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z')
INSTANCE_RE = re.compile(r'[A-Za-z0-9._-]{1,253}:[0-9]{1,5}\Z')
TRACE_ID_RE = re.compile(r'(?i)\btrace[_-]?id\b[^0-9a-fA-F]{0,12}([0-9a-fA-F]{32})\b')
TRACEPARENT_RE = re.compile(
    r'(?i)\btraceparent\b[^0-9a-fA-F]{0,12}00-([0-9a-fA-F]{32})-[0-9a-fA-F]{16}-[0-9a-fA-F]{2}\b')

NOT_HEALTH = ' This does not establish service health.'
WINDOW_LIMITATIONS = {
    'INVALID_ALERT_TIMESTAMP': 'The alert start time could not be parsed, so no log or trace query was issued.' + NOT_HEALTH,
    'WINDOW_OUTSIDE_LOOKBACK': 'The incident ended before the bounded lookback, so historical raw logs and traces were not queried.' + NOT_HEALTH,
    'WINDOW_IN_FUTURE': 'The alert start time is after the collector clock, so no telemetry query was issued.' + NOT_HEALTH,
    'EMPTY_WINDOW': 'The bounded window collapsed to an empty interval, so no telemetry query was issued.' + NOT_HEALTH,
}
BUDGET_LIMITATION = (
    'The collector stopped before this read because the stage deadline or the deterministic request cap was reached. '
    'Unfetched telemetry is unknown, not healthy.'
)
FAILED_LIMITATION = 'The telemetry query failed, so there are no rows to interpret.' + NOT_HEALTH
EMPTY_LOG_LIMITATION = (
    'The log query succeeded and returned no lines. No matching lines do not establish service health or exclude an incident.'
)
EMPTY_TRACE_SEARCH_LIMITATION = (
    'The Tempo search succeeded and returned no traces. No hits do not establish that the service is healthy or that no trace exists.'
)


def _epoch(value):
    """Whole Unix seconds for an ISO-8601 timestamp. Zero-year Alertmanager ends are open."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or value.startswith('0001-'):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return int(delta.total_seconds())


def absolute_window(starts_at, ends_at, now):
    """Absolute query bounds around the incident, clipped to now and lookback.

    ``start`` and ``end`` are the Unix seconds actually eligible for queries.
    Fractional input seconds are truncated toward zero. An incident that ends
    before the lookback, or starts after now, yields a reason and no bounds.
    """
    collected_at = int(now)
    started = _epoch(starts_at)
    base = {'collected_at': collected_at, 'start': None, 'end': None,
            'requested_start': None, 'requested_end': None,
            'incident_start': started, 'incident_end': None,
            'span_capped': False, 'clipped_to_lookback': False, 'clipped_to_now': False,
            'end_ignored_reason': None, 'reason': None}
    if started is None:
        base['reason'] = 'INVALID_ALERT_TIMESTAMP'
        return base
    ended = _epoch(ends_at)
    if ended is not None and ended < started:
        base['end_ignored_reason'] = 'END_BEFORE_START'
        ended = None
    incident_end = collected_at if ended is None else ended
    base['incident_end'] = incident_end
    earliest = collected_at - MAX_LOOKBACK_SECONDS
    if started > collected_at:
        base['reason'] = 'WINDOW_IN_FUTURE'
        return base
    if incident_end < earliest:
        base['reason'] = 'WINDOW_OUTSIDE_LOOKBACK'
        return base
    clipped_start = min(max(started, earliest), collected_at)
    clipped_end = min(max(incident_end, clipped_start), collected_at)
    requested_start = clipped_start - WINDOW_LEAD_SECONDS
    requested_end = clipped_end + WINDOW_TRAIL_SECONDS
    applied_start = max(earliest, requested_start)
    applied_end = min(collected_at, requested_end)
    span_capped = False
    if applied_end - applied_start > MAX_WINDOW_SECONDS:
        applied_start = applied_end - MAX_WINDOW_SECONDS
        span_capped = True
    base.update({
        'start': applied_start, 'end': applied_end,
        'requested_start': requested_start, 'requested_end': requested_end,
        'span_capped': span_capped,
        'clipped_to_lookback': applied_start > requested_start,
        'clipped_to_now': applied_end < requested_end,
    })
    if applied_end <= applied_start:
        base['reason'] = 'EMPTY_WINDOW'
        base['start'] = None
        base['end'] = None
    return base


def _rfc3339(epoch):
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (OverflowError, OSError, ValueError):
        return None


def _deadline_remaining():
    deadline = getattr(REQUEST_DEADLINE, 'at', None)
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _stable_id(prefix, *parts):
    raw = json.dumps(parts, sort_keys=True, separators=(',', ':'), default=str).encode()
    return prefix + '-' + hashlib.sha256(raw).hexdigest()[:16]


def _safe_name(name):
    if re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,70}\.json', name):
        return name
    return 'telemetry.json'


def grafana_explore_url(public_base, datasource_uid, query_text, start, end, query_field):
    """Grafana Explore URL with a datasource UID and an absolute millisecond range."""
    query = {
        'refId': 'A',
        'datasource': {'type': datasource_uid, 'uid': datasource_uid},
        query_field: query_text,
    }
    if datasource_uid in ('loki', 'prometheus'):
        query['queryType'] = 'range'
        query['editorMode']='code'
        if datasource_uid=='prometheus':query.update(range=True,instant=False)
    elif datasource_uid == 'tempo':
        query['queryType'] = 'traceId' if re.fullmatch(r'[a-f0-9]{32}', query_text) else 'traceql'
    pane = {
        'datasource': datasource_uid,
        'queries': [query],
        'range': {'from': str(int(start) * 1000), 'to': str(int(end) * 1000)},
    }
    encoded = urllib.parse.urlencode([
        ('orgId', '1'),
        ('schemaVersion', '1'),
        ('panes', json.dumps({'spi': pane}, separators=(',', ':'), sort_keys=True)),
        # Anonymous Viewer otherwise silently lands on the home dashboard.
        ('forceLogin', 'true'),
    ])
    return public_base.rstrip('/') + '/explore?' + encoded


def _public_base(url):
    if not isinstance(url, str):
        return None
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        return None
    if parts.query or parts.fragment or parts.path not in ('', '/'):
        return None
    host = parts.hostname
    if parts.port not in (None, 443):
        host += ':' + str(parts.port)
    return 'https://' + host


def _one_label(labels, keys, pattern, field, rejected):
    """Return one validated label, or None. Present invalid aliases are not used."""
    present = []
    for key in keys:
        if not isinstance(labels, dict) or key not in labels:
            continue
        value = labels.get(key)
        if value is None or value == '':
            continue
        present.append((key, value))
    if not present:
        return None
    bad = [key for key, value in present if not (isinstance(value, str) and pattern.fullmatch(value))]
    good = []
    for key, value in present:
        if isinstance(value, str) and pattern.fullmatch(value) and value not in good:
            good.append(value)
    for key in bad:
        rejected.append({'label': key, 'reason': 'UNSAFE_LABEL'})
    if bad:
        return None
    if len(good) > 1:
        rejected.append({'label': field, 'reason': 'LABEL_CONFLICT'})
        return None
    return good[0] if good else None


def _service_label(labels, rejected):
    value = _one_label(labels, ('service', 'service_name', 'app', 'deployment', 'k8s_deployment_name'),
                       SERVICE_RE, 'service', rejected)
    if value and UNIT_RE.fullmatch(value):
        rejected.append({'label': 'service', 'reason': 'SYSTEMD_UNIT_NOT_A_TRACE_SERVICE'})
        return None
    return value


def _nodenames(result):
    names = []
    if not isinstance(result, dict):
        return names
    data = result.get('data')
    rows = data.get('result') if isinstance(data, dict) else None
    if isinstance(rows, list):
        for row in rows:
            metric = row.get('metric') if isinstance(row, dict) else None
            if isinstance(metric, dict) and isinstance(metric.get('nodename'), str):
                names.append(metric['nodename'])
        return names
    series = result.get('series')
    if isinstance(series, list):
        for row in series:
            item = row.get('labels') if isinstance(row, dict) else None
            if isinstance(item, dict) and isinstance(item.get('nodename'), str):
                names.append(item['nodename'])
    return names


def _unique(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _evidence_hosts(evidence):
    names = []
    for query in evidence.get('queries') or []:
        if not isinstance(query, dict) or query.get('source') != 'node_identity':
            continue
        names.extend(_nodenames(query.get('result')))
    return _unique(names)


def normalize_trace_id(value):
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if re.fullmatch(r'[0-9a-f]{32}', text):
        return text
    if re.fullmatch(r'[0-9a-f]{16}', text):
        return text.zfill(32)
    return None


def trace_ids_in_text(text):
    if not isinstance(text, str):
        return []
    found = []
    for pattern in (TRACEPARENT_RE, TRACE_ID_RE):
        for match in pattern.finditer(text):
            found.append((match.start(1), match.group(1).lower()))
    found.sort()
    return _unique(value for _, value in found)


def _line_epoch(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r'[0-9]{10,19}', value):
        number = int(value)
    else:
        return None
    if number >= 10**18:
        return number // 10**9
    if number >= 10**15:
        return number // 10**6
    if number >= 10**12:
        return number // 10**3
    if number >= 10**9:
        return number
    return None


def _k8s_selector(namespace, pod, container, service):
    query = '{k8s_namespace_name=' + json.dumps(namespace) + '}'
    if pod:
        query += ' | k8s_pod_name=' + json.dumps(pod)
    if container:
        query += ' | k8s_container_name=' + json.dumps(container)
    if service and not pod:
        query += ' | service_name=' + json.dumps(service)
    return query


def _host_selector(host, unit):
    query = '{host_name=' + json.dumps(host) + '}'
    if unit:
        query += ' | service_name=' + json.dumps(unit)
    return query


def _bound_labels(labels):
    if not isinstance(labels, dict):
        return {}
    bounded = {}
    for index, (key, value) in enumerate(labels.items()):
        if index >= 20:
            break
        if isinstance(key, str) and isinstance(value, str) and len(key) <= 64 and len(value) <= 200:
            bounded[key] = value
    return bounded


def _attr_scalar(raw):
    if not isinstance(raw, dict):
        return None
    for key in ('stringValue', 'intValue', 'doubleValue', 'boolValue'):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or isinstance(value, (int, float, str)):
            if isinstance(value, str) and len(value) > ATTRIBUTE_CHAR_LIMIT:
                return value[:ATTRIBUTE_CHAR_LIMIT]
            return value
    return None


def _project_trace(payload, trace_id):
    batches = None
    if isinstance(payload, dict):
        if isinstance(payload.get('resourceSpans'), list) or isinstance(payload.get('batches'), list):
            batches = payload.get('resourceSpans') or payload.get('batches') or []
    if batches is None:
        return None
    spans = []
    scanned = 0
    scan_capped = False
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        service = None
        resource = batch.get('resource') if isinstance(batch.get('resource'), dict) else {}
        for attribute in resource.get('attributes') or []:
            if not isinstance(attribute, dict) or attribute.get('key') != 'service.name':
                continue
            value = _attr_scalar(attribute.get('value'))
            if isinstance(value, str):
                service = value[:ATTRIBUTE_CHAR_LIMIT]
        groups = batch.get('scopeSpans')
        if not isinstance(groups, list):
            groups = batch.get('instrumentationLibrarySpans') or []
        for group in groups:
            if not isinstance(group, dict):
                continue
            for raw in group.get('spans') or []:
                if scanned >= MAX_SPANS_SCANNED:
                    scan_capped = True
                    break
                scanned += 1
                if not isinstance(raw, dict):
                    continue
                own = normalize_trace_id(raw.get('traceId') or raw.get('trace_id') or '')
                if own and own != trace_id:
                    continue
                status = raw.get('status') if isinstance(raw.get('status'), dict) else {}
                code = status.get('code')
                if isinstance(code, bool) or not isinstance(code, (int, str)):
                    code = None
                attributes = {}
                omitted = 0
                truncated_attributes = False
                for attribute in raw.get('attributes') or []:
                    if not isinstance(attribute, dict) or not isinstance(attribute.get('key'), str):
                        continue
                    key = attribute['key'][:128]
                    if SECRET_KEY.search(key):
                        omitted += 1
                        continue
                    if len(attributes) >= MAX_ATTRIBUTES:
                        truncated_attributes = True
                        continue
                    value = _attr_scalar(attribute.get('value'))
                    if value is None:
                        continue
                    attributes[key] = value
                resource_context={}
                for item in (resource.get('attributes') or [])[:40]:
                    key=item.get('key','') if isinstance(item,dict)else ''
                    if key in ('service.name','service.version','host.name','container.image.name','container.image.tag','deployment.environment.name') or key.startswith('k8s.'):
                        resource_context[key]=_attr_scalar(item.get('value'))
                events=[]
                for item in (raw.get('events') or [])[:10]:
                    if not isinstance(item,dict):continue
                    event_attributes={}
                    for attribute in (item.get('attributes')or[])[:16]:
                        if not isinstance(attribute,dict):continue
                        key=attribute.get('key','')
                        if not isinstance(key,str)or SECRET_KEY.search(key):continue
                        raw_value=attribute.get('value',{})
                        value=raw_value.get('stringValue','')[:4000] if key=='exception.stacktrace' and isinstance(raw_value,dict) else _attr_scalar(raw_value)
                        event_attributes[key[:128]]=value
                    events.append({'name':str(item.get('name',''))[:180],'time_unix_nano':str(item.get('timeUnixNano',''))[:32],'attributes':event_attributes,
                                   'attributes_truncated':len(item.get('attributes')or[])>16})
                name = raw.get('name') if isinstance(raw.get('name'), str) else ''
                spans.append({
                    'trace_id': trace_id,
                    'span_id': raw.get('spanId') if isinstance(raw.get('spanId'), str) else '',
                    'parent_span_id': raw.get('parentSpanId') if isinstance(raw.get('parentSpanId'), str) else '',
                    'name': name[:180],
                    'service': service,
                    'resource_context':resource_context,
                    'events':events,
                    'events_truncated':len(raw.get('events')or[])>10,
                    'start_time_unix_nano': raw.get('startTimeUnixNano') if isinstance(raw.get('startTimeUnixNano'), str) else '',
                    'end_time_unix_nano': raw.get('endTimeUnixNano') if isinstance(raw.get('endTimeUnixNano'), str) else '',
                    'status_code': code,
                    'error': code in (2, '2', 'STATUS_CODE_ERROR'),
                    'attributes': attributes,
                    'attributes_truncated': truncated_attributes,
                    'sensitive_attributes_omitted': omitted,
                })
            if scan_capped:
                break
        if scan_capped:
            break
    spans.sort(key=lambda item: (0 if item['error'] else 1, item['start_time_unix_nano'], item['span_id'], item['name']))
    stored = spans[:MAX_TRACE_SPANS]
    return {
        'trace_id': trace_id,
        'representation': 'bounded_span_projection',
        'raw_response_retained': False,
        'span_count': len(spans),
        'stored_spans': len(stored),
        'error_span_count': sum(1 for item in spans if item['error']),
        'truncated': len(spans) > len(stored) or scan_capped or any(x['events_truncated']or x['attributes_truncated']or any(e['attributes_truncated']for e in x['events'])for x in stored),
        'span_scan_capped': scan_capped,
        'spans': stored,
        'limitation': (
            'The trace document was fetched and reduced to a bounded span projection. '
            'Credential-like attribute keys were not copied. Omitted spans are truncation, not health. '
            'A trace does not by itself establish root cause.'
        ),
    }


def _gap(source, reason, limitation, **fields):
    item = {'source': source, 'reason': reason, 'limitation': limitation}
    item.update(fields)
    return item


def _detail(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('error'), str):
        return None
    text = payload['error'].replace('\n', ' ').strip()
    if not text:
        return None
    if SECRET_KEY.search(text):
        return None
    return text[:DETAIL_CHAR_LIMIT]


class TelemetryCollector:
    def __init__(self, http, grafana_url, grafana_token, public_grafana_url=PUBLIC_GRAFANA_URL):
        if not isinstance(grafana_url, str) or not grafana_url.startswith(('http://', 'https://')):
            raise Failure('INVALID_GRAFANA_URL')
        self.http = http
        self.base = grafana_url.rstrip('/')
        self.token = grafana_token
        self.public = public_grafana_url

    def collect(self, event, existing_evidence):
        artifacts = []
        links = []
        missing = []
        seen = set()
        evidence = existing_evidence if isinstance(existing_evidence, dict) else {}
        if existing_evidence is not None and not isinstance(existing_evidence, dict):
            missing.append(_gap('existing_evidence', 'INVALID_EXISTING_EVIDENCE',
                                'Existing evidence was not a JSON object, so metric links were not read from it.' + NOT_HEALTH))
        public = _public_base(self.public)
        if public is None:
            missing.append(_gap('grafana_links', 'PUBLIC_GRAFANA_URL_INVALID',
                                'The public Grafana URL is not an https origin, so Explore links were not built.' + NOT_HEALTH))
        event = event if isinstance(event, dict) else {}
        labels = event.get('labels') if isinstance(event.get('labels'), dict) else {}
        annotations = event.get('annotations') if isinstance(event.get('annotations'), dict) else {}
        window = absolute_window(event.get('startsAt'), event.get('endsAt'), time.time())
        if public:
            self._metric_links(evidence, window, links, seen, public)
        if window.get('reason'):
            missing.append(_gap('telemetry_window', window['reason'], WINDOW_LIMITATIONS[window['reason']],
                                collected_at=window['collected_at'], incident_start=window['incident_start'],
                                incident_end=window['incident_end']))
            return {'artifacts': artifacts, 'links': links, 'missing': missing}
        self._calls = 0
        self._stop = False
        self._window = window
        if not isinstance(self.token, str) or not self.token.strip():
            missing.append(_gap('grafana', 'GRAFANA_TOKEN_UNAVAILABLE',
                                'No Grafana token was available, so upstream telemetry was not read.' + NOT_HEALTH))
            return {'artifacts': artifacts, 'links': links, 'missing': missing}
        plans, trace_service = self._plans(labels, evidence, missing, window, artifacts, links, seen, public)
        discovered = []
        anchors = []
        self._remember_trace_id(labels.get('trace_id') or annotations.get('trace_id') or labels.get('traceId'),
                                'alert', discovered, missing)
        if not plans:
            missing.append(_gap('loki_raw_logs', 'NO_SAFE_LOG_SCOPE',
                                'No validated namespace, pod, service, systemd unit, or host selector was available, '
                                'so raw logs were not queried.' + NOT_HEALTH,
                                rejected=self._rejected))
        for plan in plans:
            baseline=self._log_query(plan,'baseline','',CONTEXT_LINE_LIMIT,'backward',window['start'],window['end'],
                                     'Loki all-level log sample',artifacts,links,seen,public,missing)
            for line in baseline:
                for trace_id in trace_ids_in_text(line.get('line','')):
                    self._remember_trace_id(trace_id,'log',discovered,missing)
        for kind, line_filter, limit, title in (
                ('error', ERROR_LINE_FILTER, ERROR_LINE_LIMIT, 'Loki error logs'),
                ('stack', STACK_LINE_FILTER, STACK_LINE_LIMIT, 'Loki stack lines')):
            for plan in plans:
                lines = self._log_query(plan, kind, line_filter, limit, 'backward', window['start'], window['end'],
                                        title, artifacts, links, seen, public, missing)
                for line in lines:
                    for trace_id in trace_ids_in_text(line.get('line', '')):
                        self._remember_trace_id(trace_id, 'log', discovered, missing)
                    if kind == 'error':
                        center = _line_epoch(line.get('timestamp'))
                        if center is not None:
                            anchors.append((plan, center))
        self._search_traces(trace_service, window, discovered, artifacts, links, seen, public, missing)
        self._fetch_traces(discovered, window, artifacts, links, seen, public, missing)
        if plans:
            self._context_queries(anchors, window, artifacts, links, seen, public, missing)
        return {'artifacts': artifacts, 'links': links, 'missing': missing}

    def _plans(self, labels, evidence, missing, window, artifacts, links, seen, public):
        rejected = []
        namespace = _one_label(labels, ('namespace', 'k8s_namespace_name'), NAMESPACE_RE, 'namespace', rejected)
        pod = _one_label(labels, ('pod', 'k8s_pod_name'), POD_RE, 'pod', rejected)
        container = _one_label(labels, ('container', 'k8s_container_name'), CONTAINER_RE, 'container', rejected)
        service = _service_label(labels, rejected)
        unit = _one_label(labels, ('name', 'systemd_unit', 'unit'), UNIT_RE, 'unit', rejected)
        host = _one_label(labels, ('host_name', 'hostname', 'nodename', 'host', 'node'), HOST_RE, 'host', rejected)
        if unit:
            namespace=None;pod=None;container=None;service=None
        elif labels.get('job_name'):
            pod=evidence.get('verified_workload_pod')
            service=evidence.get('verified_workload_service')
            if pod and not POD_RE.fullmatch(pod):pod=None
            if service and not SERVICE_RE.fullmatch(service):service=None
            if not pod:
                namespace=None
                missing.append(_gap('log_scope','JOB_POD_NOT_VERIFIED','Exporter pod labels were not used as affected job pods. Raw job logs were not queried without a verified pod.'))

        if pod and any(x in pod for x in ('kube-state-metrics','node-exporter')) and not evidence.get('verified_workload_pod'):
            namespace=None;pod=None;container=None;service=None
            missing.append(_gap('log_scope','EXPORTER_POD_NOT_VERIFIED',
                                'Scrape exporter identity was not verified as an affected workload. No broader log selector was substituted.'))
        self._rejected = [{'label': item['label'], 'reason': item['reason']} for item in rejected]
        for item in rejected:
            missing.append(_gap('log_scope', item['reason'],
                                'Label ' + item['label'] + ' failed validation and was not interpolated into any query. '
                                'The selector was not widened to a broader target.' + NOT_HEALTH,
                                label=item['label']))
        k8s_blocked = any(item['label'] in ('namespace', 'k8s_namespace_name', 'pod', 'k8s_pod_name',
                                            'container', 'k8s_container_name')
                          for item in rejected)
        host_blocked = any(item['label'] in ('host', 'host_name', 'hostname', 'nodename', 'node', 'unit',
                                             'name', 'systemd_unit')
                           for item in rejected)
        plans = []
        if namespace and not k8s_blocked:
            selector = _k8s_selector(namespace, pod, container, service if not pod else None)
            note = ('Lines are limited to the validated Kubernetes namespace and pod.'
                    if pod else
                    'No validated pod label was available, so this query covers the namespace. '
                    'A namespace-wide sample does not prove individual workload health.')
            plans.append({'kind': 'k8s', 'selector': selector, 'namespace': namespace, 'pod': pod,
                          'container': container, 'service': service if not pod else None, 'scope_note': note})
        elif namespace and k8s_blocked:
            missing.append(_gap('loki_raw_logs', 'UNSAFE_LABEL',
                                'Kubernetes raw logs were not queried because a provided namespace, pod, or container '
                                'label failed validation. The query was not widened to a larger scope.' + NOT_HEALTH))
        host_name, host_source = self._resolve_host(
            labels, evidence, host if not host_blocked else None, unit, host_blocked, window, artifacts, links, seen, public, missing)
        use_host = host_name and not host_blocked and (unit or not namespace)
        if use_host:
            selector = _host_selector(host_name, unit)
            note = ('Lines are limited to the validated host and systemd unit.' if unit else
                    'Lines are limited to the validated host and are not specific to one systemd unit. '
                    'A host-wide sample does not prove unit health.')
            plans.append({'kind': 'host', 'selector': selector, 'host': host_name, 'unit': unit,
                          'host_source': host_source, 'scope_note': note})
        if evidence.get('verified_workload_service') and not unit:
            candidate=evidence['verified_workload_service']
            service=candidate if SERVICE_RE.fullmatch(candidate) else None
        service_rejected = any(
            item['label'] in ('service', 'service_name', 'app', 'deployment', 'k8s_deployment_name')
            for item in rejected)
        return plans, None if service_rejected else service

    def _resolve_host(self, labels, evidence, label_host, unit, host_blocked, window, artifacts, links, seen, public, missing):
        evidence_names = [name for name in _evidence_hosts(evidence) if HOST_RE.fullmatch(name)]
        if len(_unique(_evidence_hosts(evidence))) > 1:
            missing.append(_gap('node_identity', 'NODE_IDENTITY_AMBIGUOUS',
                                'Existing node identity returned more than one nodename, so it was not used to scope host logs.' + NOT_HEALTH,
                                series_count=len(_evidence_hosts(evidence))))
            evidence_names = []
        chosen = None
        source = None
        if len(evidence_names) == 1:
            chosen, source = evidence_names[0], 'node_identity_evidence'
        if label_host and chosen and label_host != chosen:
            missing.append(_gap('log_scope', 'HOST_SCOPE_CONFLICT',
                                'The alert host and the node identity nodename differ, so host logs were not queried.' + NOT_HEALTH))
            return None, None
        if label_host and not chosen:
            chosen, source = label_host, 'label'
        if chosen or host_blocked or not unit:
            return chosen, source
        instance = labels.get('instance') if isinstance(labels, dict) else None
        if not isinstance(instance, str) or not INSTANCE_RE.fullmatch(instance):
            if instance not in (None, ''):
                missing.append(_gap('log_scope', 'UNSAFE_LABEL',
                                    'The instance label failed validation, so node identity was not queried and host logs were not widened.' + NOT_HEALTH,
                                    label='instance'))
            else:
                missing.append(_gap('node_identity', 'NODE_IDENTITY_NOT_FOUND',
                                    'A systemd unit was present but no validated host or instance was available, so host logs were not queried.' + NOT_HEALTH))
            return None, None
        query = 'node_uname_info{instance=' + json.dumps(instance) + '}'
        payload, error = self._request('node_identity', PROMETHEUS_QUERY_PATH, [('query', query)],
                                       IDENTITY_RESPONSE_LIMIT, missing)
        self._maybe_link(links, seen, public, 'Prometheus node identity', 'prometheus', 'expr', query, window['start'], window['end'])
        if error:
            return None, None
        names = _unique(_nodenames(payload))
        if len(names) != 1 or not HOST_RE.fullmatch(names[0]):
            reason = 'NODE_IDENTITY_AMBIGUOUS' if len(names) > 1 else 'NODE_IDENTITY_NOT_FOUND'
            text = ('Node identity returned more than one nodename, so none was used to scope host logs.'
                    if len(names) > 1 else
                    'Node identity did not return one usable nodename, so host logs were not queried.')
            missing.append(_gap('node_identity', reason, text + NOT_HEALTH, series_count=len(names), query=query))
            return None, None
        artifacts.append(self._artifact(
            'node-identity', 'node-identity.json', query, PROMETHEUS_QUERY_PATH, 'prometheus', window,
            {'kind': 'node_identity', 'query': query, 'nodename': names[0], 'series_count': 1,
             'collected_at': window['collected_at'], 'collected_at_rfc3339': _rfc3339(window['collected_at']),
             'limitation': 'node_uname_info is the current scraper identity, not proof of the host at the incident start. It was used only to scope host logs.'},
            {'system': 'prometheus', 'datasource_uid': 'prometheus', 'path': PROMETHEUS_QUERY_PATH,
             'query': query, 'truncated': False}))
        return names[0], 'node_identity_query'

    def _log_query(self, plan, kind, line_filter, limit, direction, start, end, title, artifacts, links, seen, public, missing):
        if self._stopped(missing, 'loki_' + kind + '_logs'):
            return []
        query = plan['selector'] + ' ' + line_filter
        params = [('query', query), ('start', str(start)), ('end', str(end)), ('limit', str(limit)), ('direction', direction)]
        payload, error = self._request('loki_' + kind + '_logs', LOKI_RANGE_PATH, params, LOG_RESPONSE_LIMIT, missing,
                                       query=query, start=start, end=end, limit=limit, direction=direction)
        self._maybe_link(links, seen, public, title + ' (' + plan['kind'] + ')', 'loki', 'expr', query, start, end)
        if error or payload is None:
            receipt={'status':'failed','reason':error or 'NO_RESPONSE','query':query,
                     'window':self._bounds(start,end),'parameters':dict(params),'collected_at':int(time.time()),
                     'limitation':'No response snapshot was available. This is not an empty successful query.'}
            artifacts.append(self._artifact('loki-'+kind+'-'+plan['kind'],'loki-'+kind+'-receipt-'+plan['kind']+'.json',
                query,LOKI_RANGE_PATH,'loki',{'start':start,'end':end},receipt,{'system':'loki','query':query,'start':start,'end':end,'status':'failed'}))
            return []
        data = payload.get('data') if isinstance(payload.get('data'), dict) else None
        if data is None or data.get('resultType') != 'streams':
            missing.append(_gap('loki_' + kind + '_logs', 'UNEXPECTED_LOG_RESULT',
                                'Loki did not return a log stream result for this query.' + NOT_HEALTH,
                                query=query, start=start, end=end, limit=limit))
            return []
        returned = []
        malformed = 0
        for stream in data.get('result') or []:
            if not isinstance(stream, dict):
                malformed += 1
                continue
            stream_labels = _bound_labels(stream.get('stream'))
            for sample in stream.get('values') or []:
                if not isinstance(sample, (list, tuple)) or len(sample) < 2:
                    malformed += 1
                    continue
                timestamp, text = sample[0], sample[1]
                if not isinstance(text, str):
                    malformed += 1
                    continue
                stamp = timestamp if isinstance(timestamp, str) and len(timestamp) <= 32 else str(timestamp)[:32]
                returned.append({'timestamp': stamp, 'line': text, 'labels': stream_labels})
                if len(returned) >= limit:
                    break
            if len(returned) >= limit:
                break
        stored = []
        line_truncated = False
        for item in returned[:limit]:
            text = item['line']
            clipped = len(text) > LINE_CHAR_LIMIT
            line_truncated = line_truncated or clipped
            stored.append({'timestamp': item['timestamp'],
                           'line': text[:LINE_CHAR_LIMIT],
                           'line_truncated': clipped,
                           'labels': item['labels']})
        limit_reached = len(returned) >= limit
        truncated = limit_reached or line_truncated or len(returned) > len(stored)
        content = {
            'response':payload,'response_representation':'bounded_upstream_json','collected_at':int(time.time()),
            'kind': 'loki_' + kind + '_logs',
            'scope': plan['kind'],
            'query': query,
            'window': self._bounds(start, end),
            'parameters': {'limit': limit, 'direction': direction},
            'scope_fields': {key: plan.get(key) for key in ('namespace', 'pod', 'container', 'service', 'host', 'unit', 'host_source') if plan.get(key)},
            'scope_note': plan['scope_note'],
            'returned_lines': len(returned),
            'stored_lines': len(stored),
            'malformed_lines': malformed,
            'truncated': truncated,
            'truncation': {'limit_reached': limit_reached, 'line_truncated': line_truncated,
                           'line_char_limit': LINE_CHAR_LIMIT, 'limit': limit},
            'lines': stored,
            'limitation': EMPTY_LOG_LIMITATION if not stored else
            'Matching log lines are untrusted diagnostic text, not a confirmed root cause. '
            'A partial sample does not describe every log from this workload.',
        }
        source = {
            'system': 'loki', 'datasource_uid': 'loki', 'path': LOKI_RANGE_PATH,
            'query': query, 'start': start, 'end': end, 'limit': limit, 'direction': direction,
            'truncated': truncated, 'returned_lines': len(returned), 'stored_lines': len(stored),
        }
        name = 'loki-' + ('error-logs' if kind == 'error' else 'stack-lines' if kind=='stack' else 'baseline-logs') + '-' + plan['kind'] + '.json'
        artifacts.append(self._artifact('loki-' + kind + '-' + plan['kind'], name, query, LOKI_RANGE_PATH, 'loki',
                                        {'start': start, 'end': end}, content, source))
        if not stored:
            missing.append(_gap('loki_' + kind + '_logs', 'NO_MATCHING_LINES', EMPTY_LOG_LIMITATION,
                                query=query, start=start, end=end, limit=limit, direction=direction, truncated=False,
                                scope=plan['kind']))
        return stored

    def _context_queries(self, anchors, window, artifacts, links, seen, public, missing):
        chosen = []
        for plan, center in anchors:
            if any(item['kind'] == plan['kind'] and item['center'] == center for item in chosen):
                continue
            chosen.append({'plan': plan, 'center': center, 'kind': plan['kind']})
            if len(chosen) >= CONTEXT_ANCHORS:
                break
        if not chosen:
            if anchors:
                return
            missing.append(_gap('loki_context', 'NO_ERROR_ANCHOR',
                                'Nearby log context was not queried because no error line supplied a usable timestamp.' + NOT_HEALTH))
            return
        for index, item in enumerate(chosen, start=1):
            if self._stopped(missing, 'loki_context'):
                return
            start = max(window['start'], item['center'] - CONTEXT_RADIUS_SECONDS)
            end = min(window['end'], item['center'] + CONTEXT_RADIUS_SECONDS)
            if end <= start:
                missing.append(_gap('loki_context', 'EMPTY_CONTEXT_WINDOW',
                                    'The context window around an error line was empty after clipping, so it was not queried.' + NOT_HEALTH,
                                    center=item['center']))
                continue
            query = item['plan']['selector']
            params = [('query', query), ('start', str(start)), ('end', str(end)),
                      ('limit', str(CONTEXT_LINE_LIMIT)), ('direction', 'forward')]
            payload, error = self._request('loki_context', LOKI_RANGE_PATH, params, LOG_RESPONSE_LIMIT, missing,
                                           query=query, start=start, end=end, limit=CONTEXT_LINE_LIMIT,
                                           direction='forward', center=item['center'])
            self._maybe_link(links, seen, public, 'Loki log context (' + item['kind'] + ')', 'loki', 'expr', query, start, end)
            if error or payload is None:
                continue
            data = payload.get('data') if isinstance(payload.get('data'), dict) else None
            if data is None or data.get('resultType') != 'streams':
                missing.append(_gap('loki_context', 'UNEXPECTED_LOG_RESULT',
                                    'Loki did not return a log stream result for context around an error line.' + NOT_HEALTH,
                                    query=query, start=start, end=end, limit=CONTEXT_LINE_LIMIT, center=item['center']))
                continue
            stored = []
            for stream in data.get('result') or []:
                if not isinstance(stream, dict):
                    continue
                labels = _bound_labels(stream.get('stream'))
                for sample in stream.get('values') or []:
                    if not isinstance(sample, (list, tuple)) or len(sample) < 2 or not isinstance(sample[1], str):
                        continue
                    text = sample[1]
                    stored.append({'timestamp': str(sample[0])[:32], 'line': text[:LINE_CHAR_LIMIT],
                                   'line_truncated': len(text) > LINE_CHAR_LIMIT, 'labels': labels})
                    if len(stored) >= CONTEXT_LINE_LIMIT:
                        break
                if len(stored) >= CONTEXT_LINE_LIMIT:
                    break
            truncated = len(stored) >= CONTEXT_LINE_LIMIT or any(line['line_truncated'] for line in stored)
            content = {
                'response':payload,'response_representation':'bounded_upstream_json','collected_at':int(time.time()),
                'kind': 'loki_context', 'scope': item['kind'], 'query': query,
                'anchor_epoch': item['center'],
                'window': {'start': start, 'end': end, 'requested_start': item['center'] - CONTEXT_RADIUS_SECONDS,
                           'requested_end': item['center'] + CONTEXT_RADIUS_SECONDS,
                           'start_rfc3339': _rfc3339(start), 'end_rfc3339': _rfc3339(end)},
                'parameters': {'limit': CONTEXT_LINE_LIMIT, 'direction': 'forward', 'radius_seconds': CONTEXT_RADIUS_SECONDS},
                'stored_lines': len(stored), 'truncated': truncated, 'lines': stored,
                'limitation': 'Context lines are the neighboring records in a short absolute window. An empty neighborhood does not establish health.',
            }
            source = {'system': 'loki', 'datasource_uid': 'loki', 'path': LOKI_RANGE_PATH, 'query': query,
                      'start': start, 'end': end, 'limit': CONTEXT_LINE_LIMIT, 'direction': 'forward',
                      'truncated': truncated, 'center': item['center']}
            artifacts.append(self._artifact('loki-context-' + item['kind'] + '-' + str(index),
                                            'loki-context-' + item['kind'] + '-' + str(index) + '.json',
                                            query, LOKI_RANGE_PATH, 'loki', {'start': start, 'end': end}, content, source))
            if not stored:
                missing.append(_gap('loki_context', 'NO_MATCHING_LINES', content['limitation'],
                                    query=query, start=start, end=end, limit=CONTEXT_LINE_LIMIT, center=item['center']))

    def _search_traces(self, service, window, discovered, artifacts, links, seen, public, missing):
        if not service:
            missing.append(_gap('tempo_search', 'TRACE_SERVICE_NOT_AVAILABLE',
                                'Tempo was not searched by service name because no validated service label was present.' + NOT_HEALTH))
            return
        if self._stopped(missing, 'tempo_search'):
            return
        tags = 'service.name=' + service
        params = [('tags', tags), ('start', str(window['start'])), ('end', str(window['end'])), ('limit', str(SEARCH_LIMIT))]
        traceql = '{resource.service.name=' + json.dumps(service) + '}'
        payload, error = self._request('tempo_search', TEMPO_SEARCH_PATH, params, LOG_RESPONSE_LIMIT, missing,
                                       query=tags, start=window['start'], end=window['end'], limit=SEARCH_LIMIT)
        self._maybe_link(links, seen, public, 'Tempo service search', 'tempo', 'query', traceql, window['start'], window['end'])
        if error or payload is None:
            return
        rows = payload.get('traces')
        if rows is None and isinstance(payload.get('data'), dict):
            rows = payload['data'].get('traces')
        if not isinstance(rows, list):
            missing.append(_gap('tempo_search', 'UNEXPECTED_TRACE_RESULT',
                                'Tempo search did not return a trace list.' + NOT_HEALTH,
                                query=tags, start=window['start'], end=window['end'], limit=SEARCH_LIMIT))
            return
        found = []
        rejected = 0
        for row in rows[:SEARCH_LIMIT]:
            if not isinstance(row, dict):
                rejected += 1
                continue
            trace_id = normalize_trace_id(str(row.get('traceID') or row.get('traceId') or ''))
            if not trace_id:
                rejected += 1
                continue
            if trace_id not in found:
                found.append(trace_id)
            self._remember_trace_id(trace_id, 'search', discovered, missing)
        truncated = len(rows) >= SEARCH_LIMIT
        content = {
            'kind': 'tempo_search', 'query': tags, 'traceql': traceql,
            'window': {'start': window['start'], 'end': window['end'], 'start_rfc3339': _rfc3339(window['start']),
                       'end_rfc3339': _rfc3339(window['end'])},
            'parameters': {'tags': tags, 'start': window['start'], 'end': window['end'], 'limit': SEARCH_LIMIT},
            'trace_ids': found, 'returned': len(rows), 'rejected_ids': rejected, 'truncated': truncated,
            'limitation': EMPTY_TRACE_SEARCH_LIMITATION if not found else
            'Search hits are candidate trace IDs inside the absolute window, not a diagnosis.',
        }
        source = {'system': 'tempo', 'datasource_uid': 'tempo', 'path': TEMPO_SEARCH_PATH, 'query': tags,
                  'traceql': traceql, 'start': window['start'], 'end': window['end'], 'limit': SEARCH_LIMIT,
                  'truncated': truncated}
        artifacts.append(self._artifact('tempo-search', 'tempo-search.json', tags, TEMPO_SEARCH_PATH, 'tempo', window, content, source))
        if not found:
            missing.append(_gap('tempo_search', 'NO_MATCHING_TRACES', EMPTY_TRACE_SEARCH_LIMITATION,
                                query=tags, start=window['start'], end=window['end'], limit=SEARCH_LIMIT, truncated=truncated))

    def _fetch_traces(self, discovered, window, artifacts, links, seen, public, missing):
        if not discovered:
            if not any(item.get('reason') == 'INVALID_TRACE_ID' for item in missing):
                missing.append(_gap('tempo_trace', 'TRACE_ID_NOT_AVAILABLE',
                                    'No valid trace ID was present on the alert or discovered in the fetched logs or Tempo search.' + NOT_HEALTH))
            return
        fetched = 0
        for item in discovered:
            trace_id = item['trace_id']
            if fetched >= MAX_TRACE_FETCHES:
                missing.append(_gap('tempo_trace', 'TRACE_FETCH_BUDGET',
                                    'This trace ID was not fetched because the deterministic trace fetch cap was reached. Its absence from the artifacts is not health.',
                                    trace_id=trace_id))
                continue
            if self._stop:
                missing.append(_gap('tempo_trace', 'REQUEST_BUDGET_EXCEEDED', BUDGET_LIMITATION, trace_id=trace_id))
                continue
            path = TEMPO_TRACE_PREFIX + trace_id
            payload, error = self._request('tempo_trace', path, [], TRACE_RESPONSE_LIMIT, missing, trace_id=trace_id)
            self._maybe_link(links, seen, public, 'Tempo trace ' + trace_id[:8], 'tempo', 'query', trace_id,
                             window['start'], window['end'])
            if error or payload is None:
                continue
            fetched += 1
            projected = _project_trace(payload, trace_id)
            if projected is None:
                missing.append(_gap('tempo_trace', 'TRACE_SPAN_FORMAT_UNSUPPORTED',
                                    'The trace document did not contain a recognized span list, so no spans were invented.' + NOT_HEALTH,
                                    trace_id=trace_id))
                continue
            if projected['span_count'] == 0:
                missing.append(_gap('tempo_trace', 'TRACE_EMPTY',
                                    'The trace document was fetched and contained no spans. An empty trace does not establish health.' + NOT_HEALTH,
                                    trace_id=trace_id))
            source = {'system': 'tempo', 'datasource_uid': 'tempo', 'path': path, 'query': trace_id,
                      'trace_id': trace_id, 'start': window['start'], 'end': window['end'],
                      'truncated': projected['truncated'], 'span_count': projected['span_count'],
                      'stored_spans': projected['stored_spans']}
            artifacts.append(self._artifact('tempo-trace-' + trace_id, 'tempo-trace-' + trace_id + '.json',
                                            trace_id, path, 'tempo', window, projected, source))

    def _remember_trace_id(self, value, origin, discovered, missing):
        if value in (None, ''):
            return
        trace_id = normalize_trace_id(value) if origin != 'log' else value
        if origin == 'log':
            trace_id = value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) else None
        if not trace_id:
            if origin == 'alert':
                missing.append(_gap('tempo_trace', 'INVALID_TRACE_ID',
                                    'The alert trace ID was not a 16 or 32 digit hexadecimal value, so it was not fetched.' + NOT_HEALTH))
            return
        if not any(item['trace_id'] == trace_id for item in discovered):
            discovered.append({'trace_id': trace_id, 'origin': origin})

    def _request(self, source, path, params, response_limit, missing, **fields):
        if self._stop or self._calls >= MAX_REQUESTS:
            self._stop = True
            missing.append(_gap(source, 'REQUEST_BUDGET_EXCEEDED', BUDGET_LIMITATION, **fields))
            return None, 'REQUEST_BUDGET_EXCEEDED'
        remaining = _deadline_remaining()
        if remaining is not None and remaining < 1:
            self._stop = True
            missing.append(_gap(source, 'REQUEST_BUDGET_EXCEEDED', BUDGET_LIMITATION, **fields))
            return None, 'REQUEST_BUDGET_EXCEEDED'
        if not path.startswith('/api/datasources/proxy/uid/') or '..' in path or '://' in path:
            missing.append(_gap(source, 'INVALID_OBSERVABILITY_PATH',
                                'The collector refused a telemetry path outside the Loki, Tempo, and Prometheus proxies.' + NOT_HEALTH))
            return None, 'INVALID_OBSERVABILITY_PATH'
        allowed = path in (LOKI_RANGE_PATH, TEMPO_SEARCH_PATH, PROMETHEUS_QUERY_PATH) or (
            path.startswith(TEMPO_TRACE_PREFIX) and re.fullmatch(r'[0-9a-f]{32}', path[len(TEMPO_TRACE_PREFIX):]) is not None)
        if not allowed:
            missing.append(_gap(source, 'INVALID_OBSERVABILITY_PATH',
                                'The collector refused a telemetry path outside the Loki, Tempo, and Prometheus proxies.' + NOT_HEALTH))
            return None, 'INVALID_OBSERVABILITY_PATH'
        query = urllib.parse.urlencode(params) if params else ''
        url = self.base + path + ('?' + query if query else '')
        target = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.base)
        if target.scheme != base.scheme or target.netloc != base.netloc:
            missing.append(_gap(source, 'INVALID_OBSERVABILITY_PATH',
                                'The collector refused a telemetry URL that left the configured Grafana origin.' + NOT_HEALTH))
            return None, 'INVALID_OBSERVABILITY_PATH'
        self._calls += 1
        try:
            payload = self.http.request('GET', url, {'Authorization': 'Bearer ' + self.token},
                                        timeout=REQUEST_TIMEOUT_SECONDS, limit=response_limit)
        except Failure as exc:
            if exc.code == 'REQUEST_BUDGET_EXCEEDED':
                self._stop = True
                limitation = BUDGET_LIMITATION
            else:
                limitation = FAILED_LIMITATION
            missing.append(_gap(source, exc.code, limitation, **fields))
            return None, exc.code
        if not isinstance(payload, dict):
            missing.append(_gap(source, 'UNEXPECTED_RESPONSE',
                                'The telemetry response was not a JSON object, so it was not treated as data.' + NOT_HEALTH, **fields))
            return None, 'UNEXPECTED_RESPONSE'
        if payload.get('status') == 'error':
            extra = dict(fields)
            detail = _detail(payload)
            if detail:
                extra['detail'] = detail
            missing.append(_gap(source, 'OBSERVABILITY_QUERY_ERROR', FAILED_LIMITATION, **extra))
            return None, 'OBSERVABILITY_QUERY_ERROR'
        return payload, None

    def _bounds(self, start, end):
        window = getattr(self, '_window', {}) or {}
        collected = window.get('collected_at')
        return {
            'start': start, 'end': end,
            'start_rfc3339': _rfc3339(start), 'end_rfc3339': _rfc3339(end),
            'requested_start': window.get('requested_start'), 'requested_end': window.get('requested_end'),
            'collected_at': collected,
            'collected_at_rfc3339': _rfc3339(collected) if collected is not None else None,
            'span_capped': bool(window.get('span_capped')),
            'clipped_to_lookback': bool(window.get('clipped_to_lookback')),
            'clipped_to_now': bool(window.get('clipped_to_now')),
        }

    def _stopped(self, missing, source):
        remaining = _deadline_remaining()
        if not self._stop and self._calls < MAX_REQUESTS and (remaining is None or remaining >= 1):
            return False
        self._stop = True
        missing.append(_gap(source, 'REQUEST_BUDGET_EXCEEDED', BUDGET_LIMITATION))
        return True

    def _maybe_link(self, links, seen, public, title, datasource_uid, query_field, query_text, start, end):
        if not public or not isinstance(query_text, str) or query_text == '' or end <= start:
            return
        if datasource_uid not in DATASOURCE_UIDS:
            return
        from evidence_bundle import redact
        if redact(query_text)!=query_text:return
        url = grafana_explore_url(public, datasource_uid, query_text, start, end, query_field)
        if len(url) > 8000:
            return
        if url in seen:
            return
        seen.add(url)
        links.append({'title': title, 'url': url})

    def _metric_links(self, evidence, window, links, seen, public):
        evidence_window = evidence.get('window') if isinstance(evidence.get('window'), dict) else {}
        titles = {
            'prometheus_metrics': 'Prometheus metric query',
            'node_identity': 'Prometheus node identity',
            'loki_error_frequency': 'Loki error frequency',
            'host_log_error_frequency': 'Host log error frequency',
            'loki_rule_metric': 'Loki rule metric',
        }
        counts = {}
        for query in evidence.get('queries') or []:
            if not isinstance(query, dict):
                continue
            params = query.get('parameters') if isinstance(query.get('parameters'), dict) else {}
            expr = params.get('query')
            if not isinstance(expr, str) or expr == '' or len(expr) > 4000:
                continue
            path = query.get('path') if isinstance(query.get('path'), str) else ''
            match = re.search(r'/api/datasources/proxy/uid/([A-Za-z0-9_-]+)/', path)
            uid = match.group(1) if match else None
            if uid not in DATASOURCE_UIDS:
                continue
            start = params.get('start', evidence_window.get('start'))
            end = params.get('end', evidence_window.get('end'))
            if isinstance(start, bool) or isinstance(end, bool):
                continue
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or int(end) <= int(start):
                if window.get('reason') is None and window.get('start') is not None:
                    start, end = window['start'], window['end']
                else:
                    continue
            source = query.get('source') if isinstance(query.get('source'), str) else ''
            counts[source] = counts.get(source, 0) + 1
            title = titles.get(source, 'Observability query')
            if counts[source] > 1:
                title += ' (' + str(counts[source]) + ')'
            field = 'query' if uid == 'tempo' else 'expr'
            self._maybe_link(links, seen, public, title, uid, field, expr, int(start), int(end))

    def _artifact(self, prefix, name, query, path, uid, window, content, source):
        start = window.get('start') if isinstance(window, dict) else None
        end = window.get('end') if isinstance(window, dict) else None
        return {
            'id': _stable_id(prefix, query, start, end, name),
            'name': _safe_name(name),
            'media_type': 'application/json',
            'content': content,
            'source': source,
        }

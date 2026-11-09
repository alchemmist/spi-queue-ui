"""Keep Explore deep links usable with Grafana's anonymous dashboard access.

Anonymous Viewer can view the home dashboard but cannot open Explore. Without
forceLogin Grafana redirects there and drops the incident query/time. Use the
native login/return flow; do not widen anonymous or user permissions.
"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ORIGIN = 'grafana.socrat.alchemmist.xyz'

def require_explore_login(url):
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc, parts.path) != ('https', ORIGIN, '/explore'):
        return url
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != 'forceLogin']
    # Last is intentional: Grafana removes this parameter from the return URL.
    pairs.append(('forceLogin', 'true'))
    return urlunsplit(parts._replace(query=urlencode(pairs)))

def repair_markdown_links(body):
    # Only rendered Markdown links; never rewrite checksummed artifact JSON.
    return re.sub(r'\]\((https://grafana\.socrat\.alchemmist\.xyz/explore\?[^\s)]+)\)',
                  lambda m: '](' + require_explore_login(m.group(1)) + ')', body)

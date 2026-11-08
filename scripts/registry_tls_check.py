"""Bounded anonymous TLS routing diagnostics. No credentials, body or retry."""
import http.client, json, socket, time
HOST = 'harbor.socrat.alchemmist.xyz'
print(json.dumps({'dns_addresses': sorted({x[4][0] for x in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)})}))
for ip in ('109.173.40.247', '109.120.141.97'):
    c = http.client.HTTPSConnection(HOST, 443, timeout=6)
    c._create_connection = lambda address, timeout=None, source_address=None, ip=ip: socket.create_connection((ip, address[1]), timeout, source_address)
    start = time.monotonic()
    try:
        c.request('GET', '/v2/')
        with c.getresponse() as response:
            print(json.dumps({'ip':ip, 'status':response.status, 'tls_verified':True, 'seconds':round(time.monotonic()-start,3)}))
    except Exception as error:
        print(json.dumps({'ip':ip, 'error_type':type(error).__name__, 'seconds':round(time.monotonic()-start,3)}))
    finally:
        c.close()

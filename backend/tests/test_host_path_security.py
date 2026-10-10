"""Capability gates use the ASGI route path, regardless of hostile Host input."""


def test_malformed_host_cannot_escalate_worker_to_plan_mutation(client_a):
    worker = client_a.post('/api/auth/tokens',json={'name':'host-regression','scope':'coach_worker'}).json()['token']
    before = client_a.get('/api/plans').json()
    response = client_a.post('/api/plans',headers={'Authorization':'Bearer '+worker,'Host':'example.test/api/coaching/worker/claim?discard='},json={'name':'Must never create','activityType':'running','startDate':'2026-10-01'})
    assert response.status_code == 403
    assert client_a.get('/api/plans').json() == before


def test_private_api_headers_use_route_scope_despite_malformed_host(client_a):
    response = client_a.get('/api/plans',headers={'Host':'example.test/public?discard='})
    assert response.status_code == 200
    assert 'no-store' in response.headers.get('cache-control','')
    assert response.headers.get('x-robots-tag') == 'noindex, nofollow'

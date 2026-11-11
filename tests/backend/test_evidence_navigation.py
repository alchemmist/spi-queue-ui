"""Human evidence navigation and snapshots, no live observability or inference."""
import copy,json,os,stat,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.parse import urlsplit,parse_qs
from evidence_bundle import prepare_bundle,storage_objects,storage_markdown,navigation_markdown,grafana_panel_url
from telemetry import grafana_explore_url,TelemetryCollector
from test_triage import event
from test_graph_evidence import GraphEvidenceTests
import graph_evidence as graphs
from graph_contract import GraphError


class NavigationTests(unittest.TestCase):
    def test_s3_path_publishes_collected_explore_links_and_explicit_gaps(self):
        url=grafana_explore_url('https://grafana.socrat.alchemmist.xyz','loki','{namespace="api"}',100,200,'expr')
        b=prepare_bundle(event(),{'missing':[{'source':'graphs','reason':'GRAPH_CAPTURE_FAILED'}]},
                         [{'name':'logs.json','content':{'response':{'status':'success','data':{'result':[]}}}}],
                         [{'title':'Loki logs','url':url}])
        text=storage_markdown(storage_objects(b));self.assertIn(url,text);self.assertIn('GRAPH_CAPTURE_FAILED',text)
        self.assertIn('logs.json',text);self.assertIn('sign',text.lower())
        panes=json.loads(parse_qs(urlsplit(url).query)['panes'][0]);self.assertEqual(panes['spi']['range'],{'from':'100000','to':'200000'})

    def test_navigation_rejects_external_origin_userinfo_fragment_and_host_suffix(self):
        for url in ('https://evil.example/explore?x=1','https://grafana.socrat.alchemmist.xyz.evil/explore?x=1',
                    'https://user@grafana.socrat.alchemmist.xyz/explore?x=1','https://grafana.socrat.alchemmist.xyz/explore?x=1#bad'):
            self.assertNotIn(']('+url+')',navigation_markdown({'links':[{'title':'bad','url':url}]}))

    def test_panel_url_keeps_absolute_png_range_and_variables(self):
        source={'dashboard_uid':'api-panel','panel_id':12,'variables':{'namespace':'api','node':'a b'},'from_ms':100000,'to_ms':200000}
        q=parse_qs(urlsplit(grafana_panel_url(source)).query)
        self.assertEqual(q['from'],['100000']);self.assertEqual(q['to'],['200000']);self.assertEqual(q['viewPanel'],['12'])
        self.assertEqual(q['var-node'],['a b']);self.assertEqual(q['forceLogin'],['true'])

    def test_oversize_json_is_a_valid_explicit_envelope_not_chopped_json(self):
        b=prepare_bundle(event(),{},[{'name':'logs.json','content':{'lines':['x'*10000]*30}}])
        for key,raw in storage_objects(b):
            self.assertTrue(json.loads(raw)is not None)
        self.assertTrue(b['manifest']['artifacts'][0]['truncated'])
        value=json.loads(storage_objects(b)[1][1]);self.assertEqual(value['representation'],'bounded_json_excerpt')
        self.assertIn('sanitized_sha256',value)

    def test_fair_budget_keeps_later_logs_parseable_and_redacts_response(self):
        rows=[{'name':'logs-'+str(i)+'.json','content':{'password':'private-value','lines':['x'*90000]}}for i in range(12)]
        b=prepare_bundle(event(),{},rows);objects=storage_objects(b)
        self.assertEqual(len(b['manifest']['artifacts']),12)
        for key,raw in objects:
            json.loads(raw);self.assertNotIn(b'private-value',raw)
        self.assertLessEqual(sum(r['stored_chars']for r in b['manifest']['artifacts']),360000)


class PrivateSpoolTests(unittest.TestCase):
    setUp=GraphEvidenceTests.setUp

    def test_fsgroup_modes_are_tightened_without_re_render_or_content_change(self):
        captured,_=graphs.collect(self.event,self.client);ref=captured[0]['reference'];root=graphs.ARTIFACT_ROOT;d=root/ref['event_key'];png=d/(ref['render_digest']+'.png')
        os.chmod(root,0o2770);os.chmod(d,0o2770);os.chmod(d/'capture.json',0o660);os.chmod(png,0o660)
        again,missing=graphs.collect(self.event,self.client);self.assertFalse(missing);self.assertEqual(again,captured)
        self.assertEqual(self.client.render.call_count,1);self.assertEqual(graphs.read_artifact(ref),self.raw)
        for path in(root,d,d/'capture.json',png):self.assertEqual(stat.S_IMODE(path.stat().st_mode)&0o077,0)

    def test_world_access_is_rejected_not_silently_repaired(self):
        root=graphs.ARTIFACT_ROOT;root.mkdir();os.chmod(root,0o777)
        with self.assertRaises(GraphError):graphs.collect(self.event,self.client)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o777);self.client.render.assert_not_called()

    def test_foreign_group_access_is_rejected(self):
        root=graphs.ARTIFACT_ROOT;root.mkdir();os.chmod(root,0o770)
        with patch.object(graphs.os,'getegid',return_value=os.getegid()+1):
            with self.assertRaises(GraphError):graphs.collect(self.event,self.client)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o770)

    def test_png_and_dashboard_links_use_the_same_captured_window(self):
        captured,missing=graphs.collect(self.event,self.client);b=graphs.attach(prepare_bundle(self.event,{},[]),captured,missing)
        text=storage_markdown(storage_objects(b));self.assertIn('Open Grafana panel',text);self.assertIn('(PNG)',text)
        self.assertIn(str(captured[0]['source']['from_ms']),text);self.assertIn(str(captured[0]['source']['to_ms']),text)

    def test_failed_render_keeps_a_panel_link_and_visible_failure(self):
        self.client.render.side_effect=GraphError('RENDER_TIMEOUT')
        captured,missing=graphs.collect(self.event,self.client);self.assertFalse(captured)
        b=graphs.attach(prepare_bundle(self.event,{},[]),captured,missing);text=storage_markdown(storage_objects(b))
        self.assertIn('capture unavailable',text);self.assertIn('RENDER_TIMEOUT',text);self.assertIn('/d/',text)


class SnapshotTests(unittest.TestCase):
    def collect(self,labels=None,fail=False):
        e=event(start='2026-11-11T09:00:00Z');e['labels']=labels or {'alertname':'Test','namespace':'api','pod':'api-123','container':'api'}
        h=Mock()
        if fail:
            from core import Failure
            h.request.side_effect=Failure('UPSTREAM_TIMEOUT')
        else:h.request.return_value={'status':'success','data':{'resultType':'streams','result':[]}}
        with patch('telemetry.time.time',return_value=1794388200):
            result=TelemetryCollector(h,'http://grafana.internal','test-only').collect(e,{})
        return result,h

    def test_baseline_snapshot_exists_without_error_anchor_or_matching_lines(self):
        r,h=self.collect();rows=[a for a in r['artifacts']if 'baseline-logs' in a['name']]
        self.assertEqual(len(rows),1);c=rows[0]['content'];self.assertEqual(c['response']['status'],'success');self.assertEqual(c['stored_lines'],0)
        self.assertNotIn('error|exception',c['query']);self.assertLessEqual(h.request.call_count,10)

    def test_failed_read_is_a_receipt_not_an_empty_successful_snapshot(self):
        r,h=self.collect(fail=True);rows=[a for a in r['artifacts']if 'receipt' in a['name']]
        self.assertTrue(rows);self.assertEqual(rows[0]['content']['status'],'failed');self.assertNotIn('response',rows[0]['content'])

    def test_unverified_scrape_exporter_is_not_used_as_affected_pod(self):
        r,h=self.collect({'alertname':'Test','namespace':'cattle-system','pod':'kube-state-metrics-abcd','container':'kube-state-metrics'})
        self.assertTrue(any(x['reason']=='EXPORTER_POD_NOT_VERIFIED'for x in r['missing']))
        self.assertFalse(any('/loki/'in c.args[1]for c in h.request.call_args_list))

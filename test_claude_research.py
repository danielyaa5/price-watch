import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from claude_research import run,normalize,prompt,command,public_url,dashboard,ResearchError,MODEL
from watch_config import definitions,atomic

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        (self.root/'config.toml').write_text('[item.hub]\nmin_price=0\nmax_price=150\nsearch_phrases=["Thunderbolt 5 hub"]\nenabled=true\n')
        self.watch=definitions(self.root)[1]['hub'];self.watch['notes']='PRIVATE NOTE';self.watch['alert_approvals']={'email':{'secret':'PRIVATE'}}
        self.evidence={'observed_urls':{'https://example.com/hub','https://example.com/sale'},'web_searches':2,'model':MODEL,'effort':'high'}
        self.output={'watches':[{'watch_id':'hub','summary':'Research summary','deals':[{'title':'Thunderbolt 5 hub','url':'https://example.com/hub','store':'Example','price':149,'condition':'new','evidence':'Public listing'}], 'sales':[{'title':'Holiday sale','url':'https://example.com/sale','store':'Example','status':'expected','starts_on':None,'ends_on':None,'details':'Not announced yet'}]}]}
    def test_prompt_excludes_private_notes_channels_and_approvals(self):
        self.assertNotIn('PRIVATE',prompt([self.watch]))
        args=command('/test/claude');self.assertIn('WebSearch,WebFetch',args);self.assertIn('high',args);self.assertNotIn('--dangerously-skip-permissions',args)
    def test_unobserved_urls_and_unsafe_urls_cannot_be_published(self):
        self.output['watches'][0]['deals'].append(dict(self.output['watches'][0]['deals'][0],url='https://example.com/invented'))
        report=normalize(self.output,[self.watch],self.evidence)
        self.assertEqual(len(report['hub']['deals']),1)
        self.assertEqual(report['hub']['sales'][0]['status'],'expected')
        for url in ['javascript:alert(1)','https://127.0.0.1/a','https://router.local/a','https://user:pass@example.com','https://example.com/?token=secret']:self.assertIsNone(public_url(url))
    def test_unknown_condition_cannot_be_a_qualifying_deal(self):
        self.output['watches'][0]['deals'][0]['condition']='unknown'
        r=normalize(self.output,[self.watch],self.evidence)['hub']['deals'][0]
        self.assertFalse(r['within_budget']);self.assertIn('condition',r['filter_result'])
    def test_success_due_guard_failure_retention_and_dashboard_allowlist(self):
        runner=lambda watches:(self.output,self.evidence)
        self.assertEqual(run(self.root,runner=runner)['status'],'ok')
        with patch('claude_research.invoke',side_effect=AssertionError('not due')):
            self.assertEqual(run(self.root,runner=lambda _:self.fail('not due'))['status'],'not_due')
        path=self.root/'state/claude_research/results.json';saved=json.loads(path.read_text());saved['scan']['attempt_epoch']=0;saved['reports']['hub']['credential']='PRIVATE';atomic(path,saved)
        def failing(_):raise ResearchError('Model unavailable')
        self.assertEqual(run(self.root,runner=failing)['status'],'error')
        d=dashboard(self.root,{'hub':self.watch});self.assertEqual(len(d['reports']['hub']['deals']),1);self.assertNotIn('PRIVATE',json.dumps(d))
    def test_missing_watch_is_failure_not_empty_success(self):
        with self.assertRaises(ResearchError):normalize({'watches':[]},[self.watch],self.evidence)

    def test_cli_evidence_only_accepts_search_results_and_requested_main_model(self):
        from claude_research import parse_response
        events=[{'type':'system','subtype':'init','model':MODEL},
         {'type':'assistant','message':{'content':[{'type':'tool_use','id':'search','name':'WebSearch','input':{'query':'hub'}}]}},
         {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'search','content':'Found https://example.com/hub'}]}},
         {'type':'assistant','message':{'content':[{'type':'tool_use','id':'output','name':'StructuredOutput','input':{}}]}},
         {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'output','content':'https://example.com/invented'}]}},
         {'type':'result','subtype':'success','is_error':False,'structured_output':self.output,'modelUsage':{MODEL:{},'claude-haiku-helper':{}}}]
        output,evidence=parse_response('\n'.join(map(json.dumps,events)),0)
        self.assertEqual(evidence['observed_urls'],{'https://example.com/hub'})
        self.assertEqual(evidence['web_searches'],1)
        events[0]['model']='claude-sonnet-other'
        with self.assertRaises(ResearchError):parse_response('\n'.join(map(json.dumps,events)),0)

    def test_scheduled_research_is_due_every_eight_hours_even_after_failure(self):
        import time
        from claude_research import snapshot
        path=self.root/'state/claude_research/results.json'
        runner=lambda watches:(self.output,self.evidence)
        for status in ('ok','error'):
            with patch('claude_research.time.time',return_value=100000):
                atomic(path,{'scan':{'status':status,'attempt_epoch':100000-8*3600+1}})
                self.assertEqual(run(self.root,runner=lambda _:self.fail('Too early'))['status'],'not_due')
                atomic(path,{'scan':{'status':status,'attempt_epoch':100000-8*3600}})
                self.assertEqual(run(self.root,runner=runner)['status'],'ok')
        self.assertEqual(snapshot(self.root)['interval_hours'],8)

    def agy_events(self,model='gemini-3.8-flash-high',tool='read_url_content',status='SUCCESS'):
        step=lambda i,name,params,state='DONE':{'event':'step_update','step_update':{'step_index':i,'state':state,'step_type':'tool','tool_name':name,'tool_info':{'name':name,'parameters':params}}}
        events=[{'event':'init','init':{'model':model}},
         step(1,'search_web',{'query':'hub'},'ACTIVE'),step(1,'search_web',{'query':'hub'}),
         step(2,tool,{'Url':'https://example.com/hub'}),
         {'event':'result','result':{'status':status,'response':'```json\n'+json.dumps(self.output)+'\n```','num_turns':1}}]
        return '\n'.join(map(json.dumps,events))

    def test_antigravity_is_the_default_engine_with_web_tools_only(self):
        from claude_research import agy_command,engine,snapshot
        self.assertEqual(engine(self.root),'agy')
        args=agy_command('/test/agy',[self.watch])
        self.assertEqual(args[args.index('--model')+1],'gemini-3.8-flash-high')
        self.assertEqual(args[:3],['/usr/bin/sandbox-exec','-p',args[2]]);self.assertIn('mcp_config',args[2])
        self.assertIn('plan',args);self.assertIn('--json-schema',args);self.assertNotIn('--dangerously-skip-permissions',args)
        self.assertNotIn('PRIVATE',args[-1]);self.assertIn('read_url_content',args[-1])
        self.assertEqual(snapshot(self.root)['model'],'gemini-3.8-flash-high')
        atomic(self.root/'research-settings.json',{'enabled':True,'engine':'claude'})
        self.assertEqual(snapshot(self.root)['model'],MODEL)

    def test_antigravity_evidence_counts_searches_and_pages_it_read(self):
        from claude_research import parse_agy
        output,evidence=parse_agy(self.agy_events(),0)
        self.assertEqual(output,self.output);self.assertEqual(evidence['web_searches'],1)
        self.assertEqual(evidence['observed_urls'],{'https://example.com/hub'})
        for bad in (self.agy_events(model='gemini-3.8-flash-low'),self.agy_events(tool='run_command'),self.agy_events(status='ERROR')):
            with self.assertRaises(ResearchError):parse_agy(bad,0)

    def test_links_antigravity_did_not_read_are_checked(self):
        from claude_research import confirm_links
        statuses={'https://example.com/sale':'blocked','https://example.com/gone':None}
        self.output['watches'][0]['deals'].append(dict(self.output['watches'][0]['deals'][0],url='https://example.com/gone'))
        with patch('claude_research.link_status',side_effect=lambda url:statuses[url]) as check:
            observed,unconfirmed=confirm_links(self.output,{'https://example.com/hub'})
        self.assertEqual(sorted(c.args[0] for c in check.call_args_list),['https://example.com/gone','https://example.com/sale'])
        self.assertEqual(observed,{'https://example.com/hub','https://example.com/sale'});self.assertEqual(unconfirmed,{'https://example.com/sale'})
        report=normalize(self.output,[self.watch],{**self.evidence,'observed_urls':observed,'unconfirmed_urls':unconfirmed})['hub']
        self.assertEqual(len(report['deals']),1);self.assertFalse(report['deals'][0]['link_unconfirmed']);self.assertTrue(report['sales'][0]['link_unconfirmed'])

    def test_link_check_refuses_private_hosts(self):
        from claude_research import link_status
        with patch('claude_research.socket.getaddrinfo',return_value=[(0,0,0,'',('192.168.1.1',443))]):
            self.assertIsNone(link_status('https://example.com/hub'))

    def test_grounding_redirects_become_store_urls_or_are_dropped(self):
        from claude_research import resolve_grounding,confirm_links
        g='https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ'
        self.output['watches'][0]['deals'][0]['url']=g+'1'
        self.output['watches'][0]['sales'][0]['url']=g+'2'
        targets={g+'1':'https://example.com/hub',g+'2':None}
        with patch('claude_research.redirect_target',side_effect=lambda u:targets[u]):
            read=resolve_grounding(self.output,{g+'1'})
        self.assertEqual(read,{'https://example.com/hub'})
        self.assertEqual(self.output['watches'][0]['deals'][0]['url'],'https://example.com/hub')
        self.assertIsNone(self.output['watches'][0]['sales'][0]['url'])
        with patch('claude_research.link_status',side_effect=AssertionError('read pages need no check')):
            observed,_=confirm_links(self.output,read)
        report=normalize(self.output,[self.watch],{**self.evidence,'observed_urls':observed})['hub']
        self.assertEqual([d['url'] for d in report['deals']],['https://example.com/hub']);self.assertEqual(report['sales'],[])

    def test_antigravity_deals_need_a_page_it_read(self):
        report=normalize(self.output,[self.watch],{**self.evidence,'read_urls':set()})['hub']['deals'][0]
        self.assertFalse(report['within_budget']);self.assertIn('not read',report['filter_result'])
        report=normalize(self.output,[self.watch],{**self.evidence,'read_urls':{'https://example.com/hub'}})['hub']['deals'][0]
        self.assertTrue(report['within_budget'])

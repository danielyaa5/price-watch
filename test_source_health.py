import json, tempfile, unittest
from pathlib import Path
from source_health import configure, observe, dashboard, observe_report, message

class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'config.toml').write_text('[item.studio]\nsearch_phrases=["Mac Studio"]\nmax_price=4000\n[item.ams]\nsearch_phrases=["AMS"]\nmax_price=200\n')
        self.calls=[]
    def sender(self,c,m):self.calls.append((c,m));return 'receipt'
    def approve(self):configure({'channels':{'email':True,'ntfy':True},'approve':True},self.root)
    def check(self,stamp,status='error',ids=('studio',),notify=True,sender=None):
        observe(self.root,'facebook',{k:{'checked_at':stamp,'status':status,'error':'authentication required SECRET'} for k in ids},notify,self.sender if sender is None else sender)
    def test_approval_required(self):
        with self.assertRaises(ValueError):configure({'channels':{'email':True,'ntfy':True}},self.root)
        self.check('2026-01-01');self.assertFalse(self.calls)
    def test_failure_dedup_recovery_and_new_outage(self):
        self.approve();self.check('2026-01-01');self.check('2026-01-02');self.assertEqual(len(self.calls),2)
        self.check('2026-01-03','ok');self.check('2026-01-04','ok');self.assertEqual(len(self.calls),4)
        self.assertIn('recovered',self.calls[-1][1]['title']);self.check('2026-01-05');self.assertEqual(len(self.calls),6)
        self.assertNotIn('SECRET',json.dumps(dashboard(self.root)))
    def test_partial_watch_scan_cannot_claim_recovery(self):
        self.approve();self.check('2026-01-01',ids=('studio','ams'));self.check('2026-01-02','ok');self.assertEqual(len(self.calls),2)
        self.check('2026-01-03','ok',ids=('ams',));self.assertEqual(len(self.calls),4)
    def test_manual_quiet_then_scheduled_sends_current_failure(self):
        self.approve();self.check('2026-01-01',notify=False);self.assertFalse(self.calls)
        self.check('2026-01-02');self.assertEqual(len(self.calls),2)
        self.assertEqual(dashboard(self.root)['events'][0]['delivery']['email']['status'],'sent')
    def test_uncertain_never_retried_or_recovered(self):
        self.approve()
        def fail(c,m):self.calls.append((c,m));raise TimeoutError()
        self.check('2026-01-01',sender=fail);self.check('2026-01-02');self.check('2026-01-03','ok');self.assertEqual(len(self.calls),2)
    def test_no_recovery_if_failure_was_not_sent(self):
        self.check('2026-01-01');self.approve();self.check('2026-01-02','ok');self.assertFalse(self.calls)
    def test_old_check_and_partial_are_not_recovery(self):
        self.approve();self.check('2026-01-02');self.check('2026-01-01','ok');self.check('2026-01-03','partial');self.assertEqual(len(self.calls),2)
    def test_pause_does_not_send_recovery(self):
        self.approve();self.check('2026-01-01')
        from unittest.mock import patch
        with patch('source_health.active',return_value={}):self.check('2026-01-02','ok')
        self.assertEqual(len(self.calls),2)
    def test_report_hook(self):
        observe_report(self.root,'facebook',{'status':'error','finished_at':'2026-01-01','error':'authentication required'},'studio',False)
        event=dashboard(self.root)['events'][0];self.assertEqual(event['watches'],['Mac Studio']);self.assertEqual(event['problem'],'Sign-in required.')
    def test_disable_channel_independently(self):
        self.approve();configure({'channels':{'email':False,'ntfy':True}},self.root);self.check('2026-01-01');self.assertEqual([c for c,m in self.calls],['ntfy'])

if __name__=='__main__':unittest.main()

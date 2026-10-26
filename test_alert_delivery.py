import copy, json, tempfile, unittest
from pathlib import Path
from alert_delivery import process, connect, status, message
from watch_config import definitions, mutate_watch, channel_settings

class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'config.toml').write_text('[item.studio]\nsearch_phrases=["Mac Studio"]\nmin_price=3000\nmax_price=4000\n[watch.studio]\nnotify=true\n')
        self.w=definitions(self.root)[1]['studio'];self.w.update(sources=['facebook','ebay','offerup','retail'],condition='any',retail_stores=['universe','expercom','printed'])
        self.calls=[]
    def save(self,w=None):
        mutate_watch({'version':definitions(self.root)[2]['version'],'id':'studio','watch':w or self.w},self.root)
        self.w=definitions(self.root)[1]['studio'];return self.w
    def row(self,id='one',source='offerup'):
        return {'id':id,'category':'match','source':source,'title':'Mac Studio M2 Ultra 128GB 2TB','price':'$3900','location':'Pasadena','post_url':'https://offerup.com/item/detail/'+id}
    def sender(self,c,m):self.calls.append((c,m));return 'receipt'
    def test_used_watch_alerts_from_its_eligible_sources(self):
        self.w['condition']='used'
        self.save();settings=channel_settings(self.w)
        self.assertEqual(settings['email']['sources'],['facebook','ebay','offerup'])
        process(self.root,self.w,'offerup',[self.row()],True,self.sender);self.assertFalse(self.calls)
    def test_channel_baselines_dedup_and_independence(self):
        self.w['channels']={'email':True,'ntfy':False};self.save()
        process(self.root,self.w,'offerup',[self.row()],True,self.sender);self.assertFalse(self.calls)
        process(self.root,self.w,'offerup',[self.row(),self.row('two')],True,self.sender);self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][0],'email')
        process(self.root,self.w,'offerup',[self.row('two')],True,self.sender);self.assertEqual(len(self.calls),1)
        # Turning ntfy on baselines ntfy only; email keeps sending new IDs.
        self.w['channels']['ntfy']=True;self.save()
        process(self.root,self.w,'offerup',[self.row('two'),self.row('three')],True,self.sender);self.assertEqual(len(self.calls),2)
        process(self.root,self.w,'offerup',[self.row('four')],True,self.sender);self.assertEqual(len(self.calls),4)
    def test_uncertain_channel_is_held_other_channel_succeeds(self):
        self.save();process(self.root,self.w,'offerup',[],True,self.sender)
        def sender(c,m):
            self.calls.append(c)
            if c=='email':raise TimeoutError()
            return 'received'
        counts=process(self.root,self.w,'offerup',[self.row()],True,sender)
        self.assertEqual(counts['uncertain'],1);self.assertEqual(counts['sent'],1)
        process(self.root,self.w,'offerup',[self.row()],True,sender);self.assertEqual(len(self.calls),2)
        self.assertEqual(status(self.root)['studio:email']['uncertain'],1)
    def test_manual_checks_never_create_delivery_ledger(self):
        self.save();process(self.root,self.w,'offerup',[self.row()],False,self.sender)
        self.assertFalse((self.root/'state/alerts').exists());self.assertFalse(self.calls)
    def test_pause_during_send_stops_next_channel(self):
        self.save();process(self.root,self.w,'offerup',[],True,self.sender)
        def sender(c,m):
            self.calls.append(c);w=copy.deepcopy(self.w);w['enabled']=False;self.save(w);return 'yes'
        process(self.root,self.w,'offerup',[self.row()],True,sender);self.assertEqual(self.calls,['email'])
    def test_criteria_change_saves_and_baselines_quietly(self):
        self.save();process(self.root,self.w,'offerup',[self.row()],True,self.sender)
        w=copy.deepcopy(self.w);w['max_price']=3950;self.save(w)
        process(self.root,self.w,'offerup',[self.row(),self.row('two')],True,self.sender);self.assertFalse(self.calls)
        process(self.root,self.w,'offerup',[self.row('three')],True,self.sender);self.assertEqual(len(self.calls),2)
    def test_alert_epoch_cannot_be_set_from_watch_body(self):
        self.w['channels']={'email':False,'ntfy':False};self.save()
        self.w['channels']['email']=True;self.w['alert_epochs']={'email':'fake'};self.w['alert_approvals']={'email':{'fingerprint':'fake','sources':['offerup'],'epoch':'fake'}}
        self.save();self.assertNotEqual(channel_settings(self.w)['email']['approval']['epoch'],'fake')
    def test_retail_stores_have_separate_baselines(self):
        self.save()
        r={**self.row(source='retail'),'store_id':'universe'}
        process(self.root,self.w,'retail',[r],True,self.sender,bucket='retail:universe')
        r['store_id']='expercom';process(self.root,self.w,'retail',[r],True,self.sender,bucket='retail:expercom');self.assertFalse(self.calls)
    def test_ebay_delivery_state_deleted_with_account(self):
        from ebay_store import save_check, delete_account, owner_hash
        from watch_config import fingerprint, now
        (self.root/'ebay-private.json').write_text(json.dumps({'verification_token':'test-only'}))
        self.save();row={**self.row('ebay_1','ebay'),'_owner':owner_hash(self.root,'seller')}
        save_check(self.root,self.w,[row],{'status':'ok','checked_at':now(),'fingerprint':fingerprint(self.w)})
        process(self.root,self.w,'ebay',[row],True,self.sender)
        delete_account(self.root,{'notificationId':'test','data':{'username':'seller'}})
        with connect(self.root,'ebay') as db:self.assertEqual(db.execute('SELECT count(*) FROM alert_delivery').fetchone()[0],0)
    def test_price_templates_include_cost_basis(self):
        r=self.row(source='retail');self.assertIn('before shipping and tax',message(self.w,r)['body'])
        r.update(source='ebay',total=3950);self.assertIn('$3,950.00 including shipping',message(self.w,r)['subject'])
    def test_successful_collector_snapshot_reaches_selected_channels(self):
        from alert_delivery import collect_alerts
        from offerup_collect import save_check
        from watch_config import now, fingerprint
        from unittest.mock import patch
        self.save();stamp=now();report={'started_at':stamp,'status':'ok'}
        check={'status':'ok','checked_at':now(),'fingerprint':fingerprint(self.w)}
        save_check(self.root,self.w,[self.row()],check)
        with patch('alert_delivery.send',side_effect=lambda root,c,m:self.sender(c,m)):
            collect_alerts(self.root,'offerup',report,notify=True)
            self.assertFalse(self.calls)
            check['checked_at']=now();save_check(self.root,self.w,[self.row('new')],check)
            collect_alerts(self.root,'offerup',report,notify=True)
            self.assertEqual([x[0] for x in self.calls],['email','ntfy'])
            collect_alerts(self.root,'offerup',report,notify=True);self.assertEqual(len(self.calls),2)
    def test_failed_scan_does_not_alert_from_retained_results(self):
        from alert_delivery import collect_alerts
        from offerup_collect import save_check
        from watch_config import now, fingerprint
        from unittest.mock import patch
        self.save()
        save_check(self.root,self.w,[self.row()],{'status':'error','checked_at':now(),'fingerprint':fingerprint(self.w)})
        with patch('alert_delivery.send',side_effect=lambda root,c,m:self.sender(c,m)):
            collect_alerts(self.root,'offerup',{'status':'error','started_at':now()},notify=True)
        self.assertFalse(self.calls);self.assertFalse((self.root/'state/alerts').exists())

if __name__=='__main__':unittest.main()

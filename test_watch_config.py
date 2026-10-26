import copy, json, tempfile, unittest
from pathlib import Path
from watch_config import definitions, load_config, mutate_watch, annotate, fingerprint, channel_settings, outside_area, validate

class WatchConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'config.toml').write_text('[marketplace.facebook]\nradius=40\n[item.studio]\nsearch_phrases=["Mac Studio"]\nmax_price=4000\nmin_price=3000\ntitle_keywords="Mac AND Studio"\n[watch.studio]\nnotify=true\n')
        self.w=definitions(self.root)[1]['studio'];self.w['alerts']=False
    def save(self,w=None,**kwargs):return mutate_watch({'version':definitions(self.root)[2]['version'],'watch':w or self.w,**kwargs},self.root)
    def test_budget_query_and_profile_edit_reaches_scanner(self):
        self.w.update(max_price=3600,queries=['Mac Studio Ultra'],profile='custom',required=['128GB'])
        self.save(id='studio');c=load_config(self.root)
        self.assertEqual(c['item']['studio']['max_price'],3600);self.assertEqual(c['item']['studio']['search_phrases'],['Mac Studio Ultra']);self.assertNotIn('title_keywords',c['item']['studio']);self.assertEqual(c['_definitions']['studio']['required'],['128GB'])
    def test_switching_a_channel_on_alerts_every_selected_source(self):
        self.w.update(channels={'email':True,'ntfy':False},sources=['facebook','ebay','craigslist']);self.save(id='studio')
        settings=channel_settings(definitions(self.root)[1]['studio'])
        self.assertTrue(settings['email']['enabled']);self.assertEqual(settings['email']['sources'],['facebook','ebay','craigslist']);self.assertFalse(settings['ntfy']['enabled'])
    def test_criteria_edit_keeps_epoch_but_reenabling_starts_a_new_baseline(self):
        self.w.update(channels={'email':True,'ntfy':True});self.save(id='studio')
        first=channel_settings(definitions(self.root)[1]['studio'])['email']['approval']['epoch']
        w=definitions(self.root)[1]['studio'];w['max_price']=3800;self.save(w,id='studio')
        self.assertEqual(channel_settings(definitions(self.root)[1]['studio'])['email']['approval']['epoch'],first)
        w=definitions(self.root)[1]['studio'];w['channels']={'email':False,'ntfy':True};self.save(w,id='studio')
        w=definitions(self.root)[1]['studio'];w['channels']={'email':True,'ntfy':True};self.save(w,id='studio')
        self.assertNotEqual(channel_settings(definitions(self.root)[1]['studio'])['email']['approval']['epoch'],first)
    def test_facebook_searches_local_pickup_only(self):
        self.assertEqual(load_config(self.root)['item']['studio']['delivery_method'],['local_pick_up'])
    def test_default_radius_is_five_miles(self):
        w=copy.deepcopy(self.w);w.pop('radius');self.assertEqual(validate(w)['radius'],5)
    def test_lower_radius_drops_listings_from_the_wider_search(self):
        self.w['radius']=40;self.save(id='studio');wide=fingerprint(definitions(self.root)[1]['studio'])
        w=definitions(self.root)[1]['studio'];w['radius']=5;self.save(w,id='studio');w=definitions(self.root)[1]['studio']
        self.assertTrue(outside_area(w,wide));self.assertFalse(outside_area(w,fingerprint(w)));self.assertTrue(outside_area(w,'unknown-legacy'))
        w['radius']=10;self.save(w,id='studio');w=definitions(self.root)[1]['studio']
        narrow=[k for k,v in w['areas'].items() if v['radius']==5][0]
        self.assertFalse(outside_area(w,narrow));self.assertTrue(outside_area(w,wide))
    def test_paused_watch_keeps_alert_switch(self):
        self.w.update(alerts=True,enabled=False);self.save(id='studio')
        c=load_config(self.root);self.assertFalse(c['item']['studio']['enabled']);self.assertTrue(c['watch']['studio']['notify'])
    def test_new_watch_and_archive_preserve_definition(self):
        result=self.save();key=result['id'];self.w['archived']=True;self.save(id=key)
        w=definitions(self.root)[1][key];self.assertTrue(w['archived']);self.assertFalse(w['enabled']);self.assertFalse(w['alerts']);self.assertTrue((self.root/'watches.previous.json').exists())
    def test_conflict_does_not_overwrite(self):
        self.save(id='studio')
        with self.assertRaisesRegex(ValueError,'another window'):mutate_watch({'version':0,'watch':self.w,'id':'studio'},self.root)
    def test_invalid_budget_does_not_write(self):
        for value in [-1,False,float('nan'),100001]:
            with self.subTest(value=value):
                w=copy.deepcopy(self.w);w['max_price']=value
                with self.assertRaises(ValueError):self.save(w,id='studio')
    def test_annotations_persist_independent_actions(self):
        for action in ('saved','dismissed'):annotate({'key':'studio:123','action':action,'value':True},self.root)
        d=json.loads((self.root/'state/annotations.json').read_text());self.assertEqual(d['studio:123'],{'saved':True,'dismissed':True})
        with self.assertRaises(ValueError):annotate({'key':'../../foo','action':'saved','value':True},self.root)
    def test_only_criteria_change_fingerprint(self):
        old=fingerprint(self.w);self.w['enabled']=False;self.w['notes']='A note';self.assertEqual(fingerprint(self.w),old);self.w['max_price']=3500;self.assertNotEqual(fingerprint(self.w),old)

class SourceConfigTests(WatchConfigTests):
    def test_ebay_only_watch_does_not_run_facebook(self):
        from watch_config import definitions, load_config, mutate_watch
        w=definitions(self.root)[1]['studio'];w.update(sources=['ebay'],alerts=False)
        mutate_watch({'version':0,'id':'studio','watch':w},self.root)
        self.assertFalse(load_config(self.root)['item']['studio']['enabled'])
    def test_edited_ams_retains_fast_title_and_accessory_filters(self):
        from watch_config import definitions, load_config, mutate_watch
        w=definitions(self.root)[1]['studio'];w.update(profile='ams',queries=['AMS 2 Pro'],alerts=False)
        mutate_watch({'version':0,'id':'studio','watch':w},self.root)
        options=load_config(self.root)['item']['studio']
        self.assertEqual(options['title_keywords'],'AMS AND Pro');self.assertIn('rack',options['antikeywords'])

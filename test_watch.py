import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from watch import reason, price_value, payload, deliver_once
import sqlite3

# Tests that build the real collector configuration use this fixture, not the local config.toml.
FIXTURE=Path(__file__).parent/'test_config.toml'
def fixture_root(tmp):
    root=Path(tmp);(root/'config.toml').write_text(FIXTURE.read_text());return root
def fixture_marketplace():
    import tempfile, watch
    from watch_config import load_config
    with tempfile.TemporaryDirectory() as tmp:
        root=fixture_root(tmp)
        with patch.object(watch,'load_config',lambda _:load_config(root)):return watch.marketplace(None)
class FilterTests(unittest.TestCase):
    def test_hub_facebook_budget_does_not_fetch_a_sixth_detail(self):
        from watch import facebook_results
        fetched=[]
        def search(item):
            for i in range(20):fetched.append(i);yield i
        market=SimpleNamespace(search=search)
        self.assertEqual(list(facebook_results(market,None,{'profile':'thunderbolt4'})),list(range(5)))
        self.assertEqual(fetched,list(range(5)))
        self.assertEqual(len(list(facebook_results(market,None,{'profile':'studio'}))),20)
    def test_thunderbolt_hubs_reject_compatibility_and_power_accessories(self):
        definition={'profile':'thunderbolt4','min_price':0,'max_price':150,'required':[],'excluded':[],'condition':'any'}
        for title in ['OWC Thunderbolt HUB with 4 X Thunderbolt 4 and 1 X USB-A','Plugable Thunderbolt 4 & USB4 Hub 60W Host Charging','Plugable 5-in-1 Thunderbolt 4 Hub + Power Adapter- Like New','Belkin Thunderbolt 4 Laptop Docking Station USB C Hub','Belkin Thunderbolt 4 5-in-1 Core Hub']:
            with self.subTest(title=title):self.assertIsNone(reason('fixture',self.listing(title,price='$100'),definition))
        for title in ['20V 6A AC Adapter for Plugable Thunderbolt 4 TBT4-HUB3C Dock','7-in-2 USB C Hub for MacBook Thunderbolt 4 Compatible','USB C Hub Thunderbolt 4 USB C Port HDMI','Cable for OWC Thunderbolt 4 Hub','OWC Thunderbolt 5 Hub']:
            with self.subTest(title=title):self.assertIsNotNone(reason('fixture',self.listing(title,price='$100'),definition))
        definition['profile']='thunderbolt5'
        self.assertIsNone(reason('fixture',self.listing('OWC Thunderbolt 5 Hub',price='$150'),definition))
        self.assertIsNotNone(reason('fixture',self.listing('OWC Thunderbolt 5 Hub',price='$150.01'),definition))
    def listing(self,title='Apple MacBook Pro 16-inch M5 Pro 64GB 1TB',price='$3,000.00',description='Working used laptop'):
        return SimpleNamespace(title=title,price=price,description=description,location='Los Angeles',post_url='https://www.facebook.com/marketplace/item/123/',id='123')
    def test_budget_boundary(self):
        self.assertIsNone(reason('macbook',self.listing()))
        self.assertEqual(reason('macbook',self.listing(price='$3,000.01')),'over maximum')
    def test_wrong_specs_and_accessories(self):
        for title in ['MacBook Pro 14-inch M5 Pro 64GB 1TB','MacBook Pro 16-inch M5 Pro 48GB 1TB','MacBook Pro 16-inch M5 Max 64GB 1TB','MacBook Pro 16-inch M5 Pro 64GB 2TB','Case for MacBook Pro 16-inch M5 Pro 64GB 1TB']:
            with self.subTest(title=title):self.assertIsNotNone(reason('macbook',self.listing(title)))
    def test_ambiguous_or_financed_prices(self):
        for price in ['$0','Free','$100/month','CA$2900','From $2500','$2,500–$3,000']:
            with self.subTest(price=price):self.assertIsNotNone(reason('macbook',self.listing(price=price)))
    def test_details_fill_short_title(self):
        self.assertIsNone(reason('macbook',self.listing(title='MacBook Pro M5 Pro',description='16.2 inch display, 64 GB unified memory, 1 TB SSD')))
    def test_wrong_title_not_rescued_by_description(self):
        self.assertIsNotNone(reason('macbook',self.listing(title='MacBook Pro 14-inch M5 Pro 48GB 1TB',description='Also compare with the 16-inch 64GB 1TB model')))
    def test_ams_complete_unit(self):
        self.assertIsNone(reason('ams',self.listing(title='Bambu Lab AMS 2 Pro',price='$200')))
        self.assertIsNone(reason('ams',self.listing(title='Bambu Lab Filament Automatic Material System 2 PRO with Filament Dryer',price='$199')))
        self.assertIsNotNone(reason('ams',self.listing(title='6pcs AMS 2Pro Guard Ceramic Filament Inlets',price='$16')))
        self.assertIsNotNone(reason('ams',self.listing(title='AMS 2 Pro Heating Unit',price='$35')))
        self.assertIsNotNone(reason('ams',self.listing(title='AMS 2 Pro bracket',price='$20')))
        self.assertIsNotNone(reason('ams',self.listing(title='AMS 2 Pro',price='$200.01')))
    def test_locked_and_broken(self):
        for word in ['activation locked','MDM locked','for parts','not working']:
            self.assertIsNotNone(reason('macbook',self.listing(description=word)))
    def test_approved_notification_format(self):
        p=payload('macbook',self.listing())
        self.assertEqual(p['message'],'Apple MacBook Pro 16-inch M5 Pro 64GB 1TB\n$3,000.00 · Los Angeles\nhttps://www.facebook.com/marketplace/item/123/')
        self.assertNotIn('Sent by',p['message'])
class UpstreamConfigTests(unittest.TestCase):
    def test_actual_upstream_and_local_filters_accept_spec_variants(self):
        from ai_marketplace_monitor.listing import Listing
        m,c=fixture_marketplace()
        item=m.get_item_config(name='macbook',marketplace='facebook',**c['item']['macbook'])
        for title in ['MacBook Pro 16-inch M5 Pro 64 GB 1 TB','Mac Book Pro 16-inch M5 Pro 64GB 1TB']:
            x=Listing(marketplace='facebook',name='macbook',id='fixture',title=title,image='',price='$2,900',post_url='https://www.facebook.com/marketplace/item/123/',location='Los Angeles',seller='fixture',condition='used',description='Working used laptop')
            with self.subTest(title=title):
                self.assertFalse(m._exclude(x,item))
                self.assertIsNone(reason('macbook',x))

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE seen (item TEXT,id TEXT,status TEXT,price TEXT,at TEXT,receipt TEXT,PRIMARY KEY(item,id))')
        self.listing=SimpleNamespace(id='abc',price='$2,900')
    def test_send_once_across_checks(self):
        calls=[]
        sender=lambda *args:calls.append(args) or 'provider-id'
        self.assertEqual(deliver_once(self.db,'macbook',self.listing,sender=sender),('sent','provider-id'))
        self.assertEqual(deliver_once(self.db,'macbook',self.listing,sender=sender),('duplicate',None))
        self.assertEqual(len(calls),1)
    def test_ambiguous_send_is_not_retried(self):
        def failed(*args):raise TimeoutError('outcome unknown')
        with self.assertRaises(TimeoutError):deliver_once(self.db,'macbook',self.listing,sender=failed)
        self.assertEqual(self.db.execute('SELECT status FROM seen').fetchone()[0],'pending')
        self.assertEqual(deliver_once(self.db,'macbook',self.listing,sender=failed),('duplicate',None))
    def test_baseline_does_not_notify_backlog(self):
        def forbidden(*args):raise AssertionError('baseline must not send')
        self.assertEqual(deliver_once(self.db,'macbook',self.listing,baseline=True,sender=forbidden),('baseline',None))
        self.assertEqual(deliver_once(self.db,'macbook',self.listing,sender=forbidden),('duplicate',None))

if __name__=='__main__':unittest.main()

class MultiChannelTests(unittest.TestCase):
    def test_one_channel_failure_does_not_block_or_repeat_other(self):
        from watch import send_channels
        db=sqlite3.connect(':memory:');listing=SimpleNamespace(id='abc');calls=[]
        def bad(*args):calls.append('ntfy');raise TimeoutError()
        def good(*args):calls.append('email');return 'mail-id'
        for _ in range(2):
            with self.assertRaises(RuntimeError):send_channels(db,'macbook',listing,{'ntfy':bad,'email':good})
        self.assertEqual(calls,['ntfy','email'])
        self.assertEqual(db.execute("SELECT status FROM channel_delivery WHERE channel='email'").fetchone()[0],'sent')
    def test_email_template(self):
        from watch import email_payload
        listing=FilterTests().listing()
        self.assertEqual(email_payload('macbook',listing),{'subject':'Price watch: MacBook Pro 16-inch M5 Pro, 64GB, 1TB at $3,000.00','body':'Apple MacBook Pro 16-inch M5 Pro 64GB 1TB\n$3,000.00 · Los Angeles\nhttps://www.facebook.com/marketplace/item/123/'})

class StudioTests(unittest.TestCase):
    def listing(self,title='Apple Mac Studio M5 Max 64GB 512GB',price='$3,500',description='Working used desktop'):
        return FilterTests().listing(title,price,description)
    def test_target_variants_and_budgets(self):
        for title in ['Mac Studio M5 Max 64GB 512GB','Mac Studio M3 Ultra 96GB 1TB','Mac Studio M2 Ultra 128GB 2TB','Mac Studio M4 Max 128GB 1TB']:
            for price in ['$3,000','$4,000']:
                self.assertIsNone(reason('studio',self.listing(title,price)))
        self.assertEqual(reason('studio',self.listing(price='$4,000.01')),'over maximum')
        self.assertEqual(reason('studio',self.listing(price='$2,999')),'below target range')
    def test_wrong_or_missing_specs(self):
        for title in ['Mac Studio M2 Ultra 64GB 1TB','Mac Studio M4 Max 64GB 1TB','Mac Studio M1 Ultra 128GB 1TB','Mac Studio M5 Max 48GB 1TB','Mac Studio M5 Max','Mac Studio M5 Max 64GB','Mac Studio M5 Max 64GB 512GB empty box','Mac Studio Display M5 Max 64GB 512GB']:
            self.assertIsNotNone(reason('studio',self.listing(title)))
        self.assertIsNotNone(reason('studio',self.listing(description='Brand new sealed')))
        self.assertIsNotNone(reason('studio',self.listing(title='Mac Studio M5 Max 48GB 1TB',description='Compare to 64GB RAM')))
        self.assertIsNotNone(reason('studio',self.listing(description='MDM locked')))
    def test_missing_specs_can_be_resolved_in_description(self):
        self.assertIsNone(reason('studio',self.listing(title='Mac Studio M3 Ultra',description='96 GB unified memory, 1TB SSD. Used, working.')))
    def test_studio_notifications_are_blocked_with_global_notify(self):
        from watch import notifications_enabled
        m,c=fixture_marketplace()
        c['watch']['studio']['notify']=False  # Explicit unapproved fixture, independent of live approval.
        self.assertFalse(notifications_enabled(c,'studio',SimpleNamespace(notify=True)))
        self.assertTrue(notifications_enabled(c,'macbook',SimpleNamespace(notify=True)))
        self.assertFalse(notifications_enabled(c,'macbook',SimpleNamespace(notify=False)))
        item=m.get_item_config(name='studio',marketplace='facebook',**c['item']['studio'])
        from ai_marketplace_monitor.listing import Listing
        x=Listing(marketplace='facebook',name='studio',id='fixture',title='Apple Mac Studio M3 Ultra 96GB 1TB',image='',price='$3,500',post_url='https://www.facebook.com/marketplace/item/123/',location='Los Angeles',seller='fixture',condition='used',description='Working used desktop')
        self.assertFalse(m._exclude(x,item))
        self.assertIsNone(reason('studio',x))

class DashboardFilterTests(unittest.TestCase):
    def test_custom_watch_and_edited_budget(self):
        definition={'profile':'custom','min_price':100,'max_price':800,'required':['camera','sony'],'excluded':['broken'],'condition':'used'}
        good=FilterTests().listing('Sony camera',price='$700')
        self.assertIsNone(reason('watch_fixture',good,definition))
        definition['max_price']=650;self.assertEqual(reason('watch_fixture',good,definition),'over maximum')
        definition['max_price']=800;definition['required']=['lens'];self.assertEqual(reason('watch_fixture',good,definition),'missing required phrase: lens')
    def test_studio_edited_budget_retains_hardware_rules(self):
        definition={'profile':'studio','min_price':2500,'max_price':4200,'required':[],'excluded':[],'condition':'used'}
        self.assertIsNone(reason('studio',StudioTests().listing(price='$4,100'),definition))
        self.assertIsNotNone(reason('studio',StudioTests().listing(title='Mac Studio M2 Ultra 64GB 1TB'),definition))
    def test_catalog_tracks_prices_and_keeps_excluded_candidates(self):
        import tempfile,json,watch
        from pathlib import Path
        old=watch.STATE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                watch.STATE=Path(tmp)
                for price,stamp in [('$800','2026-10-24T10:00:00+00:00'),('$700','2026-10-24T11:00:00+00:00')]:
                    watch.save_item_result('fixture',{'checked_at':stamp,'accepted':[{'id':'123','title':'Camera','price':price}],'rejected':[{'id':'456','title':'Broken camera','reason':'non-working'}]})
                catalog=json.loads((watch.STATE/'catalog.json').read_text());self.assertEqual(len(catalog['fixture:123']['price_history']),2);self.assertEqual(catalog['fixture:456']['category'],'candidate');self.assertEqual(catalog['fixture:123']['first_seen'],'2026-10-24T10:00:00+00:00')
            finally:watch.STATE=old

class CustomUpstreamTests(unittest.TestCase):
    def test_added_watch_is_accepted_by_real_collector_configuration(self):
        import tempfile
        from pathlib import Path
        from watch_config import definitions, mutate_watch, load_config
        m,c=fixture_marketplace()
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture_root(tmp)
            w=definitions(root)[1]['studio'];w.update(name='Camera fixture',profile='custom',queries=['Sony camera'],min_price=100.01,max_price=799.99,alerts=False,enabled=True,required=['Sony'],excluded=['broken'])
            result=mutate_watch({'version':0,'watch':w},root);config=load_config(root)
            item=m.get_item_config(name=result['id'],marketplace='facebook',**config['item'][result['id']])
            self.assertEqual(item.search_phrases,['Sony camera'])
            self.assertEqual(item.min_price,'100');self.assertEqual(item.max_price,'800')
            self.assertEqual(reason(result['id'],FilterTests().listing('Sony camera',price='$800'),config['_definitions'][result['id']]),'over maximum')
            self.assertEqual(reason(result['id'],FilterTests().listing('Sony camera',price='$100'),config['_definitions'][result['id']]),'below target range')
            self.assertIsNone(reason(result['id'],FilterTests().listing('Sony camera',price='$700'),config['_definitions'][result['id']]))

class InFlightAlertTests(unittest.TestCase):
    def test_paused_archived_edited_or_muted_watch_cannot_send_stale_scan(self):
        from watch import delivery_allowed
        import tempfile
        from watch_config import definitions
        with tempfile.TemporaryDirectory() as tmp:original=definitions(fixture_root(tmp))[1]['studio']
        self.assertTrue(delivery_allowed(original,original))
        for key,value in [('enabled',False),('archived',True),('alerts',False),('max_price',3500),('name','Different name')]:
            with self.subTest(key=key):self.assertFalse(delivery_allowed(original,{**original,key:value}))

class FacebookAreaTests(unittest.TestCase):
    def test_far_listing_is_skipped_before_it_is_opened(self):
        from unittest.mock import patch
        from watch import BoundedFacebook
        obj=BoundedFacebook.__new__(BoundedFacebook);obj.logger=None;obj.pickup_radius=5
        listing=SimpleNamespace(title='Belkin dock',location='Citrus Heights, CA')
        with patch('watch.check',return_value={'Citrus Heights, CA':'outside'}),patch('watch.FacebookMarketplace._exclude',return_value=False) as upstream:
            self.assertTrue(obj._exclude(listing,None,False));upstream.assert_not_called()
        with patch('watch.check',return_value={'Los Angeles, CA':'review'}),patch('watch.FacebookMarketplace._exclude',return_value=False) as upstream:
            self.assertFalse(obj._exclude(SimpleNamespace(title='Dock',location='Los Angeles, CA'),None,False));upstream.assert_called_once()
    def test_failed_or_oldest_watch_runs_first(self):
        import json, tempfile, watch
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'items.json').write_text(json.dumps({'a':{'status':'ok','checked_at':'2026-10-26T03:00'},'b':{'status':'ok','checked_at':'2026-10-26T02:00'},'c':{'status':'error','checked_at':'2026-10-26T03:20'}}))
            with patch.object(watch,'STATE',Path(tmp)):
                self.assertEqual([n for n,_ in watch.facebook_order({'a':{},'b':{},'c':{},'d':{}})],['c','d','b','a'])

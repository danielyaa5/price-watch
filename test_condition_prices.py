import copy
import unittest
from types import SimpleNamespace
from watch_config import validate, price_rule, search_bounds, fingerprint, condition_bucket, channel_settings

class ConditionPricesTests(unittest.TestCase):
    def setUp(self):
        self.watch=dict(name='Test hub',profile='custom',queries=['hub'],required=[],excluded=[],min_price=0,max_price=150,radius=40,sources=['ebay'],condition='any',enabled=True,archived=False,alerts=False)
    def listing(self,condition='',title='Hub'):
        return SimpleNamespace(title=title,condition=condition)
    def test_old_budget_fingerprint_and_alert_epoch_remain_unchanged(self):
        w=self.watch
        old=fingerprint(w)
        w['price_ranges']={'new':{'min':0,'max':150},'used':{'min':0,'max':150}}
        self.assertEqual(fingerprint(w),old)
        w['channels']={'email':True,'ntfy':True};w['alert_approvals']={c:{'fingerprint':old,'sources':['ebay'],'epoch':'approved'} for c in w['channels']}
        self.assertEqual(channel_settings(w)['email']['approval']['epoch'],'approved')
    def test_each_condition_enforces_own_range_and_search_covers_both(self):
        w=validate(dict(self.watch,price_ranges={'new':{'min':100,'max':200},'used':{'min':50,'max':120}}))
        self.assertEqual(search_bounds(w),(50,200))
        self.assertIsNone(price_rule(w,self.listing('new'),160))
        self.assertIn('over maximum',price_rule(w,self.listing('refurbished'),160))
        self.assertIn('below target',price_rule(w,self.listing('new'),60))
        self.assertIsNone(price_rule(w,self.listing('used'),60))
        self.assertIn('condition needs review',price_rule(w,self.listing(),100))
        self.assertEqual(condition_bucket(self.listing('Used - like new','Brand new hub')),'used')
        self.assertNotEqual(fingerprint(w),fingerprint(self.watch))
    def test_reject_invalid_ranges(self):
        for bad in ({'new':{'min':100,'max':50},'used':{'min':0,'max':150}}, {'new':{'min':0,'max':float('nan')},'used':{'min':0,'max':150}}, {'new':{'min':0,'max':True},'used':{'min':0,'max':150}}):
            with self.assertRaises(ValueError):validate(dict(self.watch,price_ranges=bad))

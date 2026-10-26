import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from places import check, verdict, cached
from watch_config import SEARCH_AREA

# A fixed center keeps these distances independent of the configured search area.
CENTER={'latitude':34.0522,'longitude':-118.2437}
TOWNS={'Echo Park, CA':[34.0782,-118.2606],'Glendale, CA':[34.1425,-118.2551],'Sacramento, CA':[38.5816,-121.4944]}

class PlacesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'state').mkdir()
        center=patch.dict(SEARCH_AREA,CENTER);center.start();self.addCleanup(center.stop)
        self.calls=[]
    def geocoder(self,names):
        self.calls.append(names)
        return {'found':{n:TOWNS[n] for n in names if n in TOWNS},'unknown':[n for n in names if n not in TOWNS]}
    def test_shipped_far_near_and_ambiguous(self):
        result=check(['Ships to you','Sacramento, CA','Echo Park, CA','Glendale, CA','Los Angeles, CA','Nowhere Town, CA',''],5,self.root,self.geocoder)
        self.assertEqual(result['Ships to you'],'outside');self.assertEqual(result['Sacramento, CA'],'outside')
        self.assertIsNone(result['Echo Park, CA']);self.assertIsNone(result['Glendale, CA'])
        for unclear in ('Los Angeles, CA','Nowhere Town, CA',''):self.assertEqual(result[unclear],'review')
        self.assertNotIn('Los Angeles, CA',self.calls[0])
    def test_towns_are_geocoded_once_including_misses(self):
        check(['Echo Park, CA','Nowhere Town, CA'],5,self.root,self.geocoder);check(['Echo Park, CA','Nowhere Town, CA'],5,self.root,self.geocoder)
        self.assertEqual(len(self.calls),1);self.assertIsNone(cached(self.root)['Nowhere Town, CA'])
    def test_throttled_names_are_retried_not_cached(self):
        check(['Echo Park, CA','Glendale, CA'],5,self.root,lambda names:{'found':{'Echo Park, CA':TOWNS['Echo Park, CA']},'unknown':[]})
        self.assertNotIn('Glendale, CA',cached(self.root));self.assertIn('Echo Park, CA',cached(self.root))
    def test_geocoder_failure_leaves_town_unknown(self):
        def broken(names):raise RuntimeError('offline')
        self.assertEqual(check(['Echo Park, CA'],5,self.root,broken)['Echo Park, CA'],'review');self.assertEqual(cached(self.root),{})
    def test_dashboard_verdict_uses_cache_only(self):
        (self.root/'state/places.json').write_text(json.dumps({'Sacramento, CA':TOWNS['Sacramento, CA']}))
        self.assertEqual(verdict('Sacramento, CA',5,cached(self.root)),'outside');self.assertEqual(verdict('Glendale, CA',5,cached(self.root)),'review')

if __name__=='__main__':unittest.main()

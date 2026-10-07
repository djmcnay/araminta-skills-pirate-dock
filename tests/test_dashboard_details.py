import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('dashboard',Path(__file__).resolve().parents[1]/'scripts'/'dock-dashboard.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class DetailsTests(unittest.TestCase):
 def test_render_measured_details(self):
  job={'job_id':'abc','gid':'def','name':'a very long full filename.mkv','status':'downloading','aria2_status':'active','progress':{'percent':25,'downloaded':1024,'total':4096},'files':[{'path':'folder/a.mkv','length':4096,'completed':1024}]}
  result=m.detail_values(job)
  self.assertEqual(result[0],job['name'])
  self.assertIn('25.0%',result[1])
  self.assertEqual(result[2]['value'],25)
  self.assertEqual(result[3],'1.0 KB / 4.0 KB')
  self.assertEqual(result[4][0][0],'folder/a.mkv')
  self.assertEqual(result[5]['value'],'Pause')
 def test_unknown_is_not_zero(self):
  result=m.detail_values({'name':'offline','status':'stopped','progress':{}})
  self.assertIn('unknown',result[1])
  self.assertFalse(result[2]['visible'])
  self.assertEqual(result[3],'— / —')
  self.assertFalse(result[5]['interactive'])
if __name__=='__main__':unittest.main()

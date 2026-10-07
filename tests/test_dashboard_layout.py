import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('dashboard',Path(__file__).resolve().parents[1]/'scripts'/'dock-dashboard.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class LayoutTests(unittest.TestCase):
 def test_bounded_table(self):
  app=m.create_dashboard()
  props=next(c['props'] for c in app.config['components'] if c['type']=='dataframe')
  self.assertEqual(props.get('max_chars'),40)
  self.assertEqual(len(props['column_widths']),len(props['headers']))
  self.assertEqual(props['column_widths'][1],'280px')
  self.assertFalse(props['wrap'])
 def test_details_selection(self):
  app=m.create_dashboard()
  components=app.config['components']
  details=next((c for c in components if c['props'].get('label')=='Selected download'),None)
  self.assertIsNotNone(details)
  table=next(c for c in components if c['type']=='dataframe')
  event=next(d for d in app.config['dependencies'] if (table['id'],'select') in map(tuple,d['targets']))
  self.assertEqual(event['outputs'],[details['id']])
  name='Fireman Sam S08E12 Charlies Big Catch 720p AMZN WEBRip AAC2 0 H 264-BTW[EZTVx.to].mkv'
  evt=m.gr.SelectData(None,{'index':[0,4],'value':'0 B','row_value':['downloading',name],'selected':True})
  self.assertEqual(m.selected_download(evt),name)
  empty=m.gr.SelectData(None,{'index':[0,1],'value':'','row_value':None,'selected':False})
  self.assertEqual(m.selected_download(empty),'Select a download to see its name.')
  props={c['id']:c['props'] for c in components}
  rows=[]
  def visit(node):
   kids=node.get('children',[])
   if len(kids)==2 and [props.get(x['id'],{}).get('scale') for x in kids]==[2,1]:rows.append(node)
   for kid in kids:visit(kid)
  visit(app.config['layout'])
  self.assertEqual(len(rows),1)
if __name__=='__main__':unittest.main()
